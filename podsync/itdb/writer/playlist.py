"""Playlists: the :class:`PlaylistRecord` model and the ``mhyp``/``mhip`` chunks it becomes.

A playlist chunk is its header, then its data objects (title, optional
description, display prefs, smart rules, settings, property plist and — for the
master playlist — the library sort indices), then one entry per track.
"""

from __future__ import annotations

import random
import struct
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from podsync.itdb.spec.layouts.entry import MHIP_HEADER_SIZE
from podsync.itdb.spec.layouts.playlist import MHYP_HEADER_SIZE
from podsync.itdb.spec.layouts.strings import MHOD100_POSITION_BODY_SIZE, MHOD_HEADER_SIZE, write_mhod_header
from podsync.itdb.spec.playlists.kinds import is_playlist_folder, is_podcast_playlist
from podsync.itdb.spec.playlists.kinds import playlist_kind_flags as _kind_word
from podsync.itdb.writer._build import clamp_u32, record_bytes
from podsync.itdb.writer.smart_rules import (
    SmartPrefs,
    SmartRuleSet,
    write_mhod50,
    write_mhod51,
    write_mhod55,
    write_mhod102,
)
from podsync.itdb.writer.sort_index import write_library_indices
from podsync.itdb.writer.strings import write_mhod_string

__all__ = [
    "EntryMeta",
    "PlaylistRecord",
    "generate_playlist_id",
    "write_master_playlist",
    "write_mhip",
    "write_mhip_podcast_group",
    "write_mhod_playlist_prefs",
    "write_mhod_position",
    "write_mhyp",
    "write_playlist",
]

_PODCAST_GROUP_HEADER = 0x100  # MHIP podcast_group_flag of a group-heading entry
_CATEGORY_TYPES_WITH_SPECIAL_FLAG = (6, 7)  # ringtones, rentals

# Default MHOD 100 (column prefs) iTunes writes for a new playlist: offset → u32.
_PREFS_TOTAL_LENGTH = 0x288
_PREFS_WORDS = {
    0x30: 0x010084, 0x34: 0x05, 0x38: 0x09, 0x3C: 0x03, 0x40: 0x120001,
    0x4C: 0x640014, 0x50: 0x01, 0x5C: 0x320014, 0x60: 0x01, 0x6C: 0x5A0014,
    0x70: 0x01, 0x7C: 0x500014, 0x80: 0x01, 0x8C: 0x7D0015, 0x90: 0x01,
}


@dataclass
class EntryMeta:
    """Per-entry details carried over from a parsed playlist so a rewrite keeps them."""

    podcast_group_flag: int = 0
    group_id: int = 0
    podcast_group_ref: int = 0
    track_persistent_id: int = 0
    mhip_persistent_id: int = 0


@dataclass
class PlaylistRecord:
    """Everything written for one playlist: ``mhyp`` fields, name, items and smart data."""

    name: str
    track_ids: list[int] = field(default_factory=list)
    playlist_id: int | None = None  # None → a fresh random id at write time
    master: bool = False
    sortorder: int = 0
    podcast_flag: int = 0
    playlist_kind_flags: int | None = None
    parent_folder_playlist_id: int = 0
    smart_prefs: SmartPrefs | None = None
    smart_rules: SmartRuleSet | None = None
    mhsd5_type: int = 0
    phase_game_flag: int = 0
    raw_mhod100: bytes | None = None
    raw_mhod102: bytes | None = None
    raw_mhod55: bytes | None = None
    playlist_description: str | None = None
    item_metadata: list[EntryMeta] | None = None

    def __post_init__(self) -> None:
        # The kind word and the legacy podcast_flag always agree after construction.
        self.playlist_kind_flags = self.podcast_flag = self.kind_flags

    @property
    def kind_flags(self) -> int:
        raw = self.playlist_kind_flags if self.playlist_kind_flags is not None else self.podcast_flag
        return _kind_word({"playlist_kind_flags": raw})

    @property
    def is_podcast(self) -> bool:
        return is_podcast_playlist(self.kind_flags)

    @property
    def is_folder(self) -> bool:
        return is_playlist_folder(self.kind_flags)

    @property
    def is_smart(self) -> bool:
        return self.smart_prefs is not None and self.smart_rules is not None


def generate_playlist_id() -> int:
    """A random 64-bit id for a new playlist."""
    return random.getrandbits(64)


# ── entries (mhip) ──────────────────────────────────────────────────


def write_mhod_position(position: int) -> bytes:
    """MHOD 100 attached to each entry: its position in the playlist."""
    body = struct.pack("<I", clamp_u32(position)) + bytes(MHOD100_POSITION_BODY_SIZE - 4)
    return write_mhod_header(100, MHOD_HEADER_SIZE + len(body)) + body


def write_mhip(
    track_id: int,
    position: int = 0,
    mhip_id: int = 0,
    timestamp: int = 0,
    podcast_group_flag: int = 0,
    podcast_group_ref: int = 0,
    track_persistent_id: int = 0,
    mhip_persistent_id: int = 0,
) -> bytes:
    """One playlist item pointing at *track_id*."""
    return record_bytes(b"mhip", MHIP_HEADER_SIZE, {
        "child_count": 1,
        "podcast_group_flag": podcast_group_flag,
        "group_id": mhip_id,
        "track_id": track_id,
        "timestamp": timestamp,
        "group_link": podcast_group_ref,
        "track_persistent_id": track_persistent_id,
        "mhip_persistent_id": mhip_persistent_id,
    }, write_mhod_position(position))


def write_mhip_podcast_group(album_name: str, group_id: int) -> bytes:
    """The heading entry of a podcast group; its only child is the show's title."""
    title = (album_name or "").strip() or "Unknown"
    return record_bytes(b"mhip", MHIP_HEADER_SIZE, {
        "child_count": 1,
        "podcast_group_flag": _PODCAST_GROUP_HEADER,
        "group_id": group_id,
        "track_id": 0,
        "timestamp": 0,
    }, write_mhod_string(1, title))


def _grouped_entries(track_ids: Sequence[int], album_of: dict, first_id: int) -> list[bytes]:
    """Podcast episodes grouped under one heading entry per show, in first-seen order."""
    shows: dict[str, list[int]] = {}
    for track_id in track_ids:
        shows.setdefault(album_of.get(track_id, "") or "", []).append(track_id)
    entries: list[bytes] = []
    next_id = first_id
    for show, episodes in shows.items():
        group_id, next_id = next_id, next_id + 1
        entries.append(write_mhip_podcast_group(show, group_id))
        for track_id in episodes:
            entries.append(write_mhip(track_id, position=next_id, mhip_id=next_id, podcast_group_ref=group_id))
            next_id += 1
    return entries


def _plain_entries(track_ids: Sequence[int], metadata: Sequence[Any] | None) -> list[bytes]:
    details = list(metadata or [])

    def detail(index: int, name: str) -> int:
        meta = details[index] if index < len(details) else None
        return int(getattr(meta, name, 0) or 0)

    return [
        write_mhip(
            track_id, position=index, mhip_id=detail(index, "group_id"), timestamp=0,
            podcast_group_flag=detail(index, "podcast_group_flag"),
            podcast_group_ref=detail(index, "podcast_group_ref"),
            track_persistent_id=detail(index, "track_persistent_id"),
            mhip_persistent_id=detail(index, "mhip_persistent_id"),
        )
        for index, track_id in enumerate(track_ids)
    ]


# ── data objects ────────────────────────────────────────────────────


def write_mhod_playlist_prefs() -> bytes:
    """The default display-preferences MHOD 100 of a playlist."""
    body = bytearray(_PREFS_TOTAL_LENGTH - MHOD_HEADER_SIZE)
    for offset, word in _PREFS_WORDS.items():
        struct.pack_into("<I", body, offset - MHOD_HEADER_SIZE, word)
    return write_mhod_header(100, _PREFS_TOTAL_LENGTH) + bytes(body)


def _prefs_object(raw_body: bytes | None) -> bytes:
    if raw_body is None:
        return write_mhod_playlist_prefs()
    return write_mhod_header(100, MHOD_HEADER_SIZE + len(raw_body)) + bytes(raw_body)


# ── the playlist chunk ──────────────────────────────────────────────


def write_mhyp(
    name,
    track_ids,
    playlist_id=None,
    master=False,
    timestamp=None,
    sortorder=0,
    podcast_flag=0,
    tracks=None,
    db_id_2=0,
    smart_prefs=None,
    smart_rules=None,
    mhsd5_type=0,
    phase_game_flag=0,
    raw_mhod100=None,
    raw_mhod102=None,
    raw_mhod55=None,
    playlist_description=None,
    item_metadata=None,
    capabilities=None,
    podcast_grouping=False,
    track_album_map=None,
    next_mhip_id_start=1,
    playlist_kind_flags=None,
    parent_folder_playlist_id=0,
) -> bytes:
    """Serialize one playlist.  Unset ids/timestamps are generated."""
    track_ids = list(track_ids or [])
    playlist_id = generate_playlist_id() if playlist_id is None else playlist_id
    timestamp = int(time.time()) if timestamp is None else timestamp
    kind = _kind_word({"playlist_kind_flags": playlist_kind_flags if playlist_kind_flags is not None else podcast_flag})

    objects = [write_mhod_string(1, name or "Playlist")]
    description = write_mhod_string(3, playlist_description) if playlist_description else b""
    if description:
        objects.append(description)
    objects.append(_prefs_object(raw_mhod100))
    if smart_prefs is not None and smart_rules is not None:
        objects += [write_mhod50(smart_prefs), write_mhod51(smart_rules)]
    if raw_mhod102 is not None:
        objects.append(write_mhod102(raw_mhod102))
    if raw_mhod55 is not None:
        objects.append(write_mhod55(raw_mhod55))
    object_count = len(objects)
    if master and tracks:
        indices, index_count = write_library_indices(tracks, capabilities)
        if indices:
            objects.append(indices)
            object_count += index_count

    if podcast_grouping and is_podcast_playlist(kind) and track_album_map is not None:
        entries = _grouped_entries(track_ids, track_album_map, next_mhip_id_start)
    else:
        entries = _plain_entries(track_ids, item_metadata)

    category = int(mhsd5_type or 0)
    phase = int(phase_game_flag or 0)
    values: dict[str, Any] = {
        "mhod_child_count": object_count,
        "mhip_child_count": len(entries),
        "master_flag": int(bool(master)),
        "timestamp": timestamp,
        "playlist_id": playlist_id,
        "string_mhod_child_count": 2 if description else 1,
        "playlist_kind_flags": kind,
        "sort_order": sortorder,
        "parent_folder_playlist_id": int(parent_folder_playlist_id or 0),
        "timestamp_2": timestamp,
    }
    if not master:
        values.update(db_id_2=db_id_2, playlist_id_2=playlist_id)
    if category:
        values["mhsd5_type"] = category
        if category in _CATEGORY_TYPES_WITH_SPECIAL_FLAG:
            values["mhsd5_special_flag"] = 1
    if phase or category:
        values["phase_game_flag"] = phase or category
    return record_bytes(b"mhyp", MHYP_HEADER_SIZE, values, b"".join(objects) + b"".join(entries))


def write_playlist(
    playlist: PlaylistRecord, db_id_2=0, podcast_grouping=False, track_album_map=None, next_mhip_id_start=1,
) -> bytes:
    """Serialize a :class:`PlaylistRecord`."""
    return write_mhyp(
        playlist.name,
        playlist.track_ids,
        playlist_id=playlist.playlist_id,
        master=playlist.master,
        sortorder=playlist.sortorder,
        podcast_flag=playlist.podcast_flag,
        db_id_2=db_id_2,
        smart_prefs=playlist.smart_prefs,
        smart_rules=playlist.smart_rules,
        mhsd5_type=playlist.mhsd5_type,
        phase_game_flag=playlist.phase_game_flag,
        raw_mhod100=playlist.raw_mhod100,
        raw_mhod102=playlist.raw_mhod102,
        raw_mhod55=playlist.raw_mhod55,
        playlist_description=playlist.playlist_description,
        item_metadata=playlist.item_metadata,
        podcast_grouping=podcast_grouping,
        track_album_map=track_album_map,
        next_mhip_id_start=next_mhip_id_start,
        playlist_kind_flags=playlist.playlist_kind_flags,
        parent_folder_playlist_id=playlist.parent_folder_playlist_id,
    )


def write_master_playlist(track_ids, db_id_2, name="iPod", tracks=None, capabilities=None, playlist_id=None) -> bytes:
    """The hidden master playlist: every track, sorted by artist, plus the library indices."""
    return write_mhyp(
        name, track_ids, playlist_id=playlist_id, master=True, sortorder=5,
        tracks=tracks, db_id_2=db_id_2, capabilities=capabilities,
    )
