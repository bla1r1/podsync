"""One call to turn an iTunesDB path into flat, inspection-friendly datasets.

Unlike :func:`podsync.library.database.load_device_library` this keeps every
dataset (albums, artists, opaque ones) and never raises: failures are logged
and reported as ``None``.  Meant for tools and diagnostics.
"""

from __future__ import annotations

import logging
import os
import struct
from typing import Any

from podsync.itdb.reader.entry import read_itdb
from podsync.itdb.spec.fields import fourcc_text
from podsync.itdb.spec.flatten import (
    collect_entry_extras,
    collect_playlist_extras,
    collect_strings,
    collect_track_extras,
    split_datasets,
)

__all__ = ["load_ipod_library"]

logger = logging.getLogger(__name__)

_PLAYLIST_DATASETS = (("mhlp", 2), ("mhlp_podcast", 3), ("mhlp_smart", 5))


def _dict_rows(data: dict[str, Any], key: str) -> list[dict]:
    rows = data.get(key)
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _flatten_tracks(data: dict[str, Any]) -> None:
    for row in _dict_rows(data, "mhlt"):
        children = row.pop("children", []) or []
        row.update(collect_strings(children))
        row.update(collect_track_extras(children))
        code = row.get("filetype")
        if isinstance(code, int) and not isinstance(code, bool) and code > 0:
            row["filetype"] = fourcc_text(code)
        if isinstance(row.get("sort_mhod_indicators"), (bytes, bytearray)):
            row["sort_mhod_indicators"] = list(row["sort_mhod_indicators"])  # JSON friendly


def _flatten_named_rows(data: dict[str, Any], key: str) -> None:
    for row in _dict_rows(data, key):
        row.update(collect_strings(row.pop("children", []) or []))


def _flatten_playlists(data: dict[str, Any]) -> None:
    for key, dataset_type in _PLAYLIST_DATASETS:
        for row in _dict_rows(data, key):
            row.setdefault("_mhsd_dataset_type", dataset_type)
            row.setdefault("_mhsd_result_key", key)
            descriptors = row.pop("mhod_children", []) or []
            row.update(collect_strings(descriptors))
            row.update(collect_playlist_extras(descriptors))
            entries = []
            for wrapper in row.pop("mhip_children", []) or []:
                entry = wrapper.get("data", wrapper) if isinstance(wrapper, dict) else wrapper
                if isinstance(entry, dict):
                    entry.update(collect_entry_extras(entry.get("children", [])))
                entries.append(entry)
            row["items"] = entries


def _header_offset(itunesdb_path: str) -> int | None:
    with open(itunesdb_path, "rb") as handle:
        head = handle.read(0x70)
    return struct.unpack_from("<i", head, 0x6C)[0] if len(head) >= 0x70 and head[:4] == b"mhbd" else None


def load_ipod_library(itunesdb_path: str, include_play_counts: bool = True) -> dict | None:
    """Flattened datasets of *itunesdb_path*, or ``None`` if it is missing or unreadable."""
    if not itunesdb_path or not os.path.exists(itunesdb_path):
        return None
    try:
        from podsync.itdb.reader.cover_links import attach_cover_refs
        from podsync.itdb.reader.onthego import load_onthego_playlists
        from podsync.itdb.reader.play_stats import apply_play_stats, read_play_stats
        from podsync.itdb.spec.clock import DeviceClock, load_device_clock, zone_changed_since_write

        itunes_dir = os.path.dirname(itunesdb_path)
        offset = _header_offset(itunesdb_path)
        device_clock = load_device_clock(os.path.dirname(os.path.dirname(itunes_dir)), database_offset=offset)
        zone_moved = zone_changed_since_write(device_clock, offset)
        parse_clock = DeviceClock.fixed_offset(offset) if zone_moved and offset is not None else device_clock

        data = split_datasets(read_itdb(itunesdb_path, time_context=parse_clock))
        _flatten_tracks(data)
        attach_cover_refs(data.get("mhlt", []), itunesdb_path)
        _flatten_named_rows(data, "mhla")
        _flatten_playlists(data)
        _flatten_named_rows(data, "mhsd_type_8")

        if include_play_counts:
            try:
                stats = read_play_stats(os.path.join(itunes_dir, "Play Counts"))
                if stats is not None:
                    apply_play_stats(data.get("mhlt", []), stats, time_context=device_clock)
            except Exception:
                logger.debug("Play Counts merge skipped", exc_info=True)
        data["playcounts_timezone_changed"] = zone_moved

        onthego = load_onthego_playlists(itunes_dir, data.get("mhlt", []))
        if onthego:
            data.setdefault("mhlp", []).extend(onthego)
        return data
    except Exception:
        logger.error("Error parsing iTunesDB", exc_info=True)
        return None
