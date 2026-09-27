"""The firmware's ``Play Counts`` file: what happened on the iPod since the last sync.

The file (magic ``mhdp``) holds one fixed-size entry per track, in database
track order.  Merging adds the new plays/skips to the stored totals, keeps the
session deltas in ``recent_*`` keys, and takes over newer timestamps, bookmarks
and on-device rating changes.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from podsync.itdb.spec.clock import DeviceClock, active_device_clock

__all__ = ["PlayStatsEntry", "apply_play_stats", "read_play_stats"]

logger = logging.getLogger(__name__)

_MAGIC = b"mhdp"
_FILE_HEADER = struct.Struct("<4sIII")  # magic, header length, entry length, entry count


@dataclass(slots=True)
class PlayStatsEntry:
    play_count: int = 0
    last_played_mac: int = 0
    bookmark_time: int = 0
    rating: int = -1  # -1 means "not changed on the device"
    skip_count: int = 0
    last_skipped_mac: int = 0

    @property
    def has_data(self) -> bool:
        return self.play_count > 0 or self.skip_count > 0 or self.rating >= 0

    @property
    def last_played_unix(self) -> int:
        return active_device_clock().mac_to_unix(self.last_played_mac)

    @property
    def last_skipped_unix(self) -> int:
        return active_device_clock().mac_to_unix(self.last_skipped_mac)

    def last_played_as_unix(self, time_context: DeviceClock) -> int:
        return time_context.mac_to_unix(self.last_played_mac)

    def last_skipped_as_unix(self, time_context: DeviceClock) -> int:
        return time_context.mac_to_unix(self.last_skipped_mac)


# Entry fields by offset; older firmware writes shorter entries, so each field
# is read only when the entry is long enough to hold it.
_ENTRY_FIELDS = (
    ("play_count", 0),
    ("last_played_mac", 4),
    ("bookmark_time", 8),
    ("rating", 12),
    ("skip_count", 20),
    ("last_skipped_mac", 24),
)


def _decode_entry(data: bytes, base: int, entry_length: int) -> PlayStatsEntry:
    entry = PlayStatsEntry()
    for name, offset in _ENTRY_FIELDS:
        if entry_length < offset + 4:
            continue
        (value,) = struct.unpack_from("<I", data, base + offset)
        if name == "rating" and value == 0:
            continue  # zero rating in this file means "unchanged", not "no stars"
        setattr(entry, name, value)
    return entry


def read_play_stats(path: str | Path) -> list[PlayStatsEntry] | None:
    """Entries of a Play Counts file, or ``None`` when it is absent or unusable."""
    path = Path(path)
    if not path.exists():
        logger.debug("No Play Counts file at %s", path)
        return None
    try:
        data = path.read_bytes()
    except OSError as exc:
        logger.warning("Could not read Play Counts file: %s", exc)
        return None
    if len(data) < _FILE_HEADER.size:
        logger.warning("Play Counts file too small (%d bytes)", len(data))
        return None
    magic, header_length, entry_length, count = _FILE_HEADER.unpack_from(data)
    if magic != _MAGIC:
        logger.warning("Play Counts file bad magic: %r (expected b'mhdp')", magic)
        return None
    needed = header_length + entry_length * count
    if len(data) < needed:
        logger.warning("Play Counts file truncated: %d bytes < expected %d", len(data), needed)
        return None
    logger.info("Play Counts: header=%d, entry_len=%d, entries=%d", header_length, entry_length, count)

    entries = [_decode_entry(data, header_length + i * entry_length, entry_length) for i in range(count)]
    logger.info(
        "Play Counts: %d / %d entries have activity", sum(e.has_data for e in entries), len(entries),
    )
    return entries


def _merge_one(track: dict[str, Any], entry: PlayStatsEntry, clock: DeviceClock) -> tuple[bool, bool, bool]:
    track["recent_playcount"] = entry.play_count
    track["recent_skipcount"] = entry.skip_count
    track["play_count"] = track.get("play_count", 0) + entry.play_count
    track["pending_play_count"] = track.get("pending_play_count", 0) + entry.play_count
    track["skip_count"] = track.get("skip_count", 0) + entry.skip_count

    rated = entry.rating >= 0 and entry.rating != track.get("rating", 0)
    if rated:
        track["app_rating"] = track.get("rating", 0)
        track["rating"] = entry.rating
    if entry.bookmark_time > 0:
        track["bookmark_time"] = entry.bookmark_time
    for mac, key, convert in (
        (entry.last_played_mac, "last_played", entry.last_played_as_unix),
        (entry.last_skipped_mac, "last_skipped", entry.last_skipped_as_unix),
    ):
        if mac > 0:
            when = convert(clock)
            if when > track.get(key, 0):
                track[key] = when
    return entry.play_count > 0, entry.skip_count > 0, rated


def apply_play_stats(
    tracks: list[dict[str, Any]], entries: list[PlayStatsEntry], *, time_context: DeviceClock | None = None,
) -> None:
    """Merge *entries* into *tracks* in place (entry *i* belongs to track *i*)."""
    clock = time_context or active_device_clock()
    paired = min(len(tracks), len(entries))
    if len(tracks) != len(entries):
        logger.warning(
            "Track count (%d) != Play Counts entry count (%d); merging first %d",
            len(tracks), len(entries), paired,
        )
    played = skipped = rerated = 0
    for track, entry in zip(tracks[:paired], entries[:paired]):
        p, s, r = _merge_one(track, entry, clock)
        played, skipped, rerated = played + p, skipped + s, rerated + r
    for track in tracks[paired:]:
        track["recent_playcount"] = track["recent_skipcount"] = 0
    logger.info(
        "Merged Play Counts: %d plays, %d skips, %d ratings across %d tracks",
        played, skipped, rerated, paired,
    )
