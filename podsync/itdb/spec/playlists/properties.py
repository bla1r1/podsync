"""Playlist descriptions and the MHOD 55 property plist that carries them.

A playlist's description can live in three places on a parsed row:

* ``playlist_description`` — the explicit, host-side value;
* the binary plist of MHOD 55 (``playlist_property_plist``), key ``description``;
* the flattened MHOD 3 string (``album``), which firmware writes as a duplicate.

Unknown plist keys are preserved: edits change ``description`` only.
"""

from __future__ import annotations

import base64
import binascii
import plistlib
from dataclasses import dataclass, field
from typing import Any

PLAYLIST_PROPERTY_KEY = "playlist_property_plist"
PLAYLIST_DESCRIPTION_KEY = "playlist_description"
PLAYLIST_DESCRIPTION_DUPLICATE_KEY = "album"

__all__ = [
    "PLAYLIST_DESCRIPTION_DUPLICATE_KEY",
    "PLAYLIST_DESCRIPTION_KEY",
    "PLAYLIST_PROPERTY_KEY",
    "PlaylistPropertyPlist",
    "normalize_playlist_description",
    "parse_playlist_property_mhod55",
    "playlist_description_from_row",
    "playlist_description_update_fields",
    "playlist_property_from_row",
    "playlist_property_raw_body_for_write",
]


def _as_bytes(raw_body: object) -> bytes | None:
    """Raw plist bytes from bytes-like input or base64 text (JSON caches); else ``None``."""
    if isinstance(raw_body, (bytes, bytearray)):
        return bytes(raw_body)
    if isinstance(raw_body, str):
        try:
            return base64.b64decode(raw_body)
        except (binascii.Error, ValueError):
            return None
    return None


def _plist_dict(raw_body: bytes) -> dict[str, Any]:
    try:
        loaded = plistlib.loads(raw_body)
    except Exception:
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}


@dataclass(slots=True)
class PlaylistPropertyPlist:
    """The decoded MHOD 55 body, keeping the original bytes for byte-exact rewrites."""

    raw_body: bytes | None = None
    plist: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw_body(cls, raw_body: bytes | bytearray | str | None) -> PlaylistPropertyPlist:
        body = _as_bytes(raw_body)
        return cls() if body is None else cls(raw_body=body, plist=_plist_dict(body))

    @classmethod
    def from_parsed(cls, value: object) -> PlaylistPropertyPlist:
        """Accept an instance, raw bytes/base64, or the parsed dict form."""
        if isinstance(value, PlaylistPropertyPlist):
            return value
        if value is None or isinstance(value, (bytes, bytearray, str)):
            return cls.from_raw_body(value)
        if not isinstance(value, dict):
            return cls()
        body = _as_bytes(value.get("raw_body"))
        stored = value.get("plist")
        plist = dict(stored) if isinstance(stored, dict) else {}
        if isinstance(value.get("description"), str):
            plist.setdefault("description", value["description"])
        if not plist and body is not None:
            return cls.from_raw_body(body)
        return cls(raw_body=body, plist=plist)

    @classmethod
    def from_description(cls, description: str, existing: PlaylistPropertyPlist | None = None) -> PlaylistPropertyPlist:
        """A fresh binary plist with *description* set and any other keys of *existing* kept."""
        plist = {**(existing.plist if existing is not None else {}), "description": description}
        return cls(raw_body=plistlib.dumps(plist, fmt=plistlib.FMT_BINARY), plist=plist)

    @property
    def description(self) -> str:
        value = self.plist.get("description")
        return value if isinstance(value, str) else ""

    def to_parsed_dict(self) -> dict[str, Any]:
        parsed: dict[str, Any] = {"raw_body": self.raw_body or b"", "plist": dict(self.plist)}
        if self.description:
            parsed["description"] = self.description
        return parsed


def parse_playlist_property_mhod55(raw_body: bytes | bytearray) -> dict[str, Any]:
    return PlaylistPropertyPlist.from_raw_body(raw_body).to_parsed_dict()


def playlist_property_from_row(row: dict) -> PlaylistPropertyPlist:
    return PlaylistPropertyPlist.from_parsed(row.get(PLAYLIST_PROPERTY_KEY))


def playlist_description_from_row(row: dict | None) -> str:
    """Explicit value first, then the plist, then the MHOD 3 duplicate; ``""`` if none."""
    if not row:
        return ""
    explicit = row.get(PLAYLIST_DESCRIPTION_KEY)
    if isinstance(explicit, str):
        return explicit
    from_plist = playlist_property_from_row(row).description
    if from_plist:
        return from_plist
    duplicate = row.get(PLAYLIST_DESCRIPTION_DUPLICATE_KEY)
    return duplicate if isinstance(duplicate, str) else ""


def normalize_playlist_description(row: dict) -> dict:
    """Write the effective description to both host-side keys (in place)."""
    description = playlist_description_from_row(row)
    if description or PLAYLIST_DESCRIPTION_KEY in row:
        row[PLAYLIST_DESCRIPTION_KEY] = row[PLAYLIST_DESCRIPTION_DUPLICATE_KEY] = description
    return row


def playlist_description_update_fields(description: str, existing_row: dict | None = None) -> dict[str, Any]:
    """Fields to merge into a row when the user sets a description.

    Returns ``{}`` when clearing a description that never existed.  An unchanged
    description keeps the original plist bytes.
    """
    existing_row = existing_row or {}
    current = playlist_property_from_row(existing_row)
    before = playlist_description_from_row(existing_row)
    if not description and not current.raw_body and not before:
        return {}
    unchanged = description == before and current.raw_body is not None
    prop = current if unchanged else PlaylistPropertyPlist.from_description(description, current)
    return {
        PLAYLIST_DESCRIPTION_KEY: description,
        PLAYLIST_DESCRIPTION_DUPLICATE_KEY: description,
        PLAYLIST_PROPERTY_KEY: prop.to_parsed_dict(),
    }


def playlist_property_raw_body_for_write(row: dict) -> bytes | None:
    """MHOD 55 bytes to write for a row, or ``None`` when the row has no property plist."""
    wanted = row.get(PLAYLIST_DESCRIPTION_KEY)
    prop = playlist_property_from_row(row)
    if isinstance(wanted, str) and wanted != prop.description:
        prop = PlaylistPropertyPlist.from_description(wanted, prop)
    if prop.raw_body is not None:
        return prop.raw_body
    if isinstance(wanted, str):
        return PlaylistPropertyPlist.from_description(wanted).raw_body
    return None
