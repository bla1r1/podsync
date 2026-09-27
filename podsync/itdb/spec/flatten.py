"""Collapse the parser's nested chunk tree into flat rows.

The reader returns ``{"children": [{"chunk_type": ..., "data": {...}}, ...]}``
wrappers all the way down.  These helpers pull out the pieces the library
layer works with: one list of rows per dataset, and the MHOD strings and
payloads attached to a track, playlist or playlist entry.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from podsync.itdb.spec.codes import (
    DATASET_RESULT_KEYS,
    MHOD_FIELD_KEYS,
    MHOD_TYPE_CHAPTER_DATA,
    MHOD_TYPE_COLUMN_SIZE_OR_ORDER,
    MHOD_TYPE_LIBRARY_PLAYLIST_INDEX,
    MHOD_TYPE_PLAYLIST_PROPERTY_PLIST,
    MHOD_TYPE_PLAYLIST_SETTINGS,
    MHOD_TYPE_SMART_PLAYLIST_DATA,
    MHOD_TYPE_SMART_PLAYLIST_RULES,
)
from podsync.itdb.spec.playlists.properties import (
    PLAYLIST_DESCRIPTION_KEY,
    PLAYLIST_PROPERTY_KEY,
    playlist_description_from_row,
)

__all__ = [
    "collect_entry_extras",
    "collect_playlist_extras",
    "collect_strings",
    "collect_track_extras",
    "split_datasets",
]


def _payloads(wrappers: Iterable[Any] | None) -> Iterator[dict]:
    """The ``data`` dicts of MHOD wrappers, skipping anything malformed."""
    for wrapper in wrappers or ():
        data = wrapper.get("data") if isinstance(wrapper, dict) else None
        if isinstance(data, dict):
            yield data


def _dataset_rows(dataset: dict, kind: int, key: str) -> list[Any]:
    """Rows of the single list chunk inside an MHSD, tagged with where they came from."""
    children = dataset.get("children", [])
    if not children:
        return []
    list_chunk = children[0]
    items = list_chunk.get("data", []) if isinstance(list_chunk, dict) else []
    rows: list[Any] = []
    for item in items if isinstance(items, list) else []:
        row = item["data"] if isinstance(item, dict) and "data" in item else item
        if isinstance(row, dict):
            row.setdefault("_mhsd_dataset_type", kind)
            row.setdefault("_mhsd_result_key", key)
        rows.append(row)
    return rows


def split_datasets(mhbd: dict) -> dict:
    """MHBD header fields plus one entry per known dataset (see ``DATASET_RESULT_KEYS``).

    Opaque datasets (Genius, type 9) come back as hex so they can be inspected or
    carried through untouched.
    """
    result: dict[str, Any] = {key: value for key, value in mhbd.items() if key != "children"}
    for wrapper in mhbd.get("children", []):
        dataset = wrapper.get("data", {})
        kind = dataset.get("dataset_type")
        key = DATASET_RESULT_KEYS.get(kind)
        if key is None:
            continue
        if "raw_payload" in dataset:
            result[key] = {
                "raw_payload_hex": bytes(dataset["raw_payload"]).hex(),
                "genius_cuid": dataset.get("genius_cuid", ""),
            }
        else:
            result[key] = _dataset_rows(dataset, kind, key)
    return result


def collect_strings(children: list) -> dict[str, str]:
    """``{record key: text}`` for every string MHOD among *children*."""
    return {
        MHOD_FIELD_KEYS[data["mhod_type"]]: data["string"]
        for data in _payloads(children)
        if "string" in data and MHOD_FIELD_KEYS.get(data.get("mhod_type"))
    }


def collect_track_extras(mhod_children: list) -> dict:
    """Non-string track attachments; currently the decoded chapter list."""
    extras: dict[str, Any] = {}
    for data in _payloads(mhod_children):
        if data.get("mhod_type") == MHOD_TYPE_CHAPTER_DATA and isinstance(data.get("data"), dict):
            extras["chapter_data"] = data["data"]
    return extras


# Binary playlist MHOD kind -> row key.  Kind 52 (library index) may repeat; kind 55 is special.
_PLAYLIST_PAYLOAD_KEYS = {
    MHOD_TYPE_SMART_PLAYLIST_DATA: "smart_playlist_data",
    MHOD_TYPE_SMART_PLAYLIST_RULES: "smart_playlist_rules",
    MHOD_TYPE_COLUMN_SIZE_OR_ORDER: "playlist_prefs",
    MHOD_TYPE_PLAYLIST_SETTINGS: "playlist_settings",
}


def collect_playlist_extras(mhod_children: list) -> dict:
    """Smart-playlist data/rules, library indices, property plist, prefs and settings."""
    extras: dict[str, Any] = {}
    for data in _payloads(mhod_children):
        if "data" not in data:
            continue
        kind, payload = data.get("mhod_type"), data["data"]
        if kind in _PLAYLIST_PAYLOAD_KEYS:
            extras[_PLAYLIST_PAYLOAD_KEYS[kind]] = payload
        elif kind == MHOD_TYPE_LIBRARY_PLAYLIST_INDEX:
            extras.setdefault("library_indices", []).append(payload)
        elif kind == MHOD_TYPE_PLAYLIST_PROPERTY_PLIST:
            extras[PLAYLIST_PROPERTY_KEY] = payload
            description = playlist_description_from_row({PLAYLIST_PROPERTY_KEY: payload})
            if description:
                extras[PLAYLIST_DESCRIPTION_KEY] = description
    return extras


def collect_entry_extras(mhod_children: list) -> dict:
    """A playlist entry's only string is the podcast group title it heads."""
    title = collect_strings(mhod_children).get("title")
    return {"podcast_group_title": title} if title is not None else {}
