"""Decode ``mhod`` data objects.

The 24-byte header names the kind; the body is one of:

* a string with a 16-byte sub-header (UTF-16LE or UTF-8),
* a bare UTF-8 podcast URL (kinds 15/16),
* QuickTime-style chapter atoms (17),
* smart-playlist preferences (50) or big-endian ``SLst`` rules (51),
* library sort index (52) and jump table (53),
* the playlist property plist (55), playlist prefs/settings (100/102).
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Callable
from typing import Any

from podsync.itdb.reader.primitives import ParseResult
from podsync.itdb.reader.walker import handles
from podsync.itdb.spec.fields import decode_fields
from podsync.itdb.spec.layouts.strings import (
    BINARY_BLOB_MHOD_TYPES,
    CHAP_ATOM,
    CHAPTER_DATA_MHOD_TYPES,
    CHAPTER_PREAMBLE_SIZE,
    HEDR_ATOM,
    MHOD52_BODY_HEADER_SIZE,
    MHOD53_BODY_HEADER_SIZE,
    MHOD53_ENTRY_SIZE,
    MHOD100_POSITION_BODY_SIZE,
    MHOD_STRING_DATA_OFFSET,
    MHOD_STRING_SUBHEADER_OFFSET,
    NAME_ATOM,
    NON_STRING_MHOD_TYPES,
    PODCAST_URL_MHOD_TYPES,
    SEAN_ATOM,
    STRING_MHOD_TYPES,
)
from podsync.itdb.spec.playlists.properties import parse_playlist_property_mhod55
from podsync.itdb.spec.smart_fields import (
    SLST_HEADER_SIZE,
    SPL_GROUP_HEADER_BYTES_OFFSET,
    SPL_GROUP_HEADER_BYTES_SIZE,
    SPL_GROUP_MARKER,
    SPL_RULE_DATA_SIZE,
    SPL_RULE_HEADER_SIZE,
    SPLFT_STRING,
    SPLFT_UNKNOWN,
    spl_get_field_type,
)

__all__ = ["decode_rule_list", "parse_data_object"]

logger = logging.getLogger(__name__)

_ENCODING_UTF8 = 2
_ATOM_HEADER = 20  # size, tag, 12 bytes of atom-specific header


def _slice(data: bytes | bytearray, start: int, length: int) -> bytes:
    return bytes(data[start:start + length]) if length > 0 else b""


# ── text ────────────────────────────────────────────────────────────


def _decode_text(data: bytes | bytearray, offset: int, body_length: int) -> dict[str, Any]:
    if body_length < 16 or len(data) < offset + MHOD_STRING_DATA_OFFSET:
        return {"string": ""}
    encoding, byte_length, unk20, unk24 = struct.unpack_from("<IIII", data, offset + MHOD_STRING_SUBHEADER_OFFSET)
    raw = _slice(data, offset + MHOD_STRING_DATA_OFFSET, byte_length)
    codec = "utf-8" if encoding == _ENCODING_UTF8 else "utf-16-le"
    return {"string": raw.decode(codec, errors="replace"), "unk_0x20": unk20, "unk_0x24": unk24}


def _decode_url(data: bytes | bytearray, body: int, length: int) -> str:
    return _slice(data, body, length).decode("utf-8", errors="replace").rstrip("\x00")


# ── chapters (MHOD 17) ──────────────────────────────────────────────


def _atom(data: bytes | bytearray, pos: int) -> tuple[int, bytes]:
    return struct.unpack_from(">I", data, pos)[0], bytes(data[pos + 4:pos + 8])


def _chapter_title(data: bytes | bytearray, pos: int, children: int, end: int) -> str:
    """The UTF-16BE ``name`` child of a ``chap`` atom, if present."""
    title = ""
    for _ in range(children):
        if end - pos < 22:
            break
        size, tag = _atom(data, pos)
        if tag == NAME_ATOM:
            chars = struct.unpack_from(">H", data, pos + 20)[0]
            if pos + 22 + chars * 2 <= end:
                title = _slice(data, pos + 22, chars * 2).decode("utf-16-be", errors="replace")
        if size <= 0:
            break
        pos += size
    return title


def _decode_chapters(data: bytes | bytearray, body: int, length: int) -> dict[str, Any]:
    if length < CHAPTER_PREAMBLE_SIZE:
        logger.warning("MHOD17 (chapter data) too short for preamble: %d bytes", length)
        return {"chapters": []}
    unk024, unk028, unk032 = struct.unpack_from("<III", data, body)
    end = body + length
    pos = body + CHAPTER_PREAMBLE_SIZE
    if end - pos < _ATOM_HEADER:
        return {"chapters": []}
    sean_size, tag = _atom(data, pos)
    if sean_size < _ATOM_HEADER or pos + sean_size > end:
        logger.warning("Chapter data: invalid 'sean' atom size: %d", sean_size)
        return {"chapters": []}
    if tag != SEAN_ATOM:
        logger.warning("Chapter data: expected 'sean' atom, got %r", tag)
        return {"chapters": []}
    # The sean atom's children are the chapters plus one trailing hedr atom.
    chapter_count = max(0, struct.unpack_from(">I", data, pos + 0x0C)[0] - 1)
    pos += _ATOM_HEADER
    chapters: list[dict[str, Any]] = []
    for _ in range(chapter_count):
        if end - pos < _ATOM_HEADER or _atom(data, pos)[1] != CHAP_ATOM:
            break
        size, _tag, start, children = struct.unpack_from(">I4sII", data, pos)
        chapters.append({"startpos": start, "title": _chapter_title(data, pos + _ATOM_HEADER, children, end)})
        pos += size if size > 0 else _ATOM_HEADER
    if end - pos >= 8 and _atom(data, pos)[1] == HEDR_ATOM:
        pos += _atom(data, pos)[0]
    return {"unk024": unk024, "unk028": unk028, "unk032": unk032, "chapters": chapters}


# ── smart playlists (MHOD 50/51) ────────────────────────────────────

_PREF_BYTES = ("live_update", "check_rules", "check_limits", "limit_type", "limit_sort")


def _decode_prefs(data, body: int, length: int) -> dict[str, Any]:
    if length < 12:
        logger.warning("MHOD50 (SPLPref) body too short: %d bytes", length)
        return {}
    prefs: dict[str, Any] = {name: data[body + i] for i, name in enumerate(_PREF_BYTES)}
    prefs["limit_value"] = struct.unpack_from("<I", data, body + 8)[0]
    for optional, position in (("match_checked_only", 0x0C), ("reverse_sort", 0x0D)):
        if length > position:
            prefs[optional] = data[body + position]
    return prefs


def decode_rule_list(data, body_offset: int, body_length: int) -> dict[str, Any]:
    """Decode an MHOD 51 body (``SLst`` magic, then rules; groups nest another ``SLst``)."""
    if body_length < SLST_HEADER_SIZE:
        logger.warning("MHOD51 (SPLRules) body too short: %d bytes", body_length)
        return {}
    magic = bytes(data[body_offset:body_offset + 4])
    if magic != b"SLst":
        logger.warning("MHOD51: expected SLst magic, got %r", magic)
        return {}
    return _decode_slst(data, body_offset, body_length)


def _decode_slst(data, start: int, length: int) -> dict[str, Any]:
    unk004, rule_count, conjunction = struct.unpack_from(">III", data, start + 4)
    end = min(start + length, len(data))
    pos = start + SLST_HEADER_SIZE
    rules: list[dict[str, Any]] = []
    for _ in range(rule_count):
        if end - pos < SPL_RULE_HEADER_SIZE:
            break
        rule, used = _decode_rule(data, pos, end)
        rules.append(rule)
        pos += used
    return {"unk004": unk004, "rule_count": rule_count, "conjunction": conjunction, "rules": rules}


_NUMERIC_RULE = struct.Struct(">QqQQqQIIIII")
_NUMERIC_KEYS = (
    "from_value", "from_date", "from_units", "to_value", "to_date", "to_units",
    "unk052", "unk056", "unk060", "unk064", "unk068",
)


def _decode_rule(data, pos: int, end: int) -> tuple[dict[str, Any], int]:
    field_id, action_id, marker = struct.unpack_from(">III", data, pos)
    header_bytes = _slice(data, pos + SPL_GROUP_HEADER_BYTES_OFFSET, SPL_GROUP_HEADER_BYTES_SIZE)
    data_length = struct.unpack_from(">I", data, pos + 0x34)[0]
    payload_at = pos + SPL_RULE_HEADER_SIZE
    have = max(0, min(data_length, end - payload_at))
    payload = _slice(data, payload_at, have)
    used = SPL_RULE_HEADER_SIZE + data_length
    rule: dict[str, Any] = {"field_id": field_id, "action_id": action_id, "data_length": data_length}

    is_group = (
        field_id == 0 and action_id == 1 and marker == SPL_GROUP_MARKER
        and data_length >= SLST_HEADER_SIZE and have == data_length and payload[:4] == b"SLst"
    )
    if is_group:
        rule.update(header_bytes=header_bytes, group_marker=marker, group=_decode_slst(data, payload_at, data_length))
        return rule, used

    kind = spl_get_field_type(field_id)
    looks_textual = kind == SPLFT_UNKNOWN and (data_length == 0 or action_id & 0x01000000)
    if kind == SPLFT_STRING or looks_textual:
        rule["string_value"] = payload.decode("utf-16-be", errors="replace") if data_length else ""
        if kind == SPLFT_UNKNOWN:
            rule["inferred_field_type"] = "string"
        return rule, used

    if have < SPL_RULE_DATA_SIZE:  # truncated numeric rule: keep the bytes for a faithful rewrite
        rule.update(header_bytes=header_bytes, group_marker=marker, raw_data=payload)
        return rule, used
    rule.update(zip(_NUMERIC_KEYS, _NUMERIC_RULE.unpack_from(payload, 0)))
    return rule, used


# ── library index (52/53), playlist prefs (100/102) ─────────────────


def _decode_sort_index(data, body: int, length: int) -> dict[str, Any]:
    if length < 8:
        logger.warning("MHOD52 (sorted index) body too short: %d bytes", length)
        return {}
    sort_type, count = struct.unpack_from("<II", data, body)
    first = body + MHOD52_BODY_HEADER_SIZE
    fits = max(0, (body + length - first) // 4)
    indices = list(struct.unpack_from(f"<{min(count, fits)}I", data, first)) if min(count, fits) else []
    return {"sort_type": sort_type, "count": count, "indices": indices}


def _decode_jump_table(data, body: int, length: int) -> dict[str, Any]:
    if length < 8:
        logger.warning("MHOD53 (jump table) body too short: %d bytes", length)
        return {}
    sort_type, count = struct.unpack_from("<II", data, body)
    end = body + length
    entries: list[dict[str, int]] = []
    pos = body + MHOD53_BODY_HEADER_SIZE
    while len(entries) < count and pos + MHOD53_ENTRY_SIZE <= end:
        letter, _pad, start, run = struct.unpack_from("<HHII", data, pos)
        entries.append({"letter_code": letter, "start": start, "count": run})
        pos += MHOD53_ENTRY_SIZE
    return {"sort_type": sort_type, "count": count, "entries": entries}


def _nonzero_words(body: bytes) -> dict[str, int]:
    """Sparse view of an opaque body: aligned non-zero u32 words, else single bytes."""
    found: dict[str, int] = {}
    covered: set[int] = set()
    for index, byte in enumerate(body):
        if byte == 0 or index in covered:
            continue
        aligned = index & ~3
        if aligned + 4 <= len(body):
            (word,) = struct.unpack_from("<I", body, aligned)
            if word:
                found[f"0x{aligned:03X}"] = word
                covered.update(range(aligned, aligned + 4))
                continue
        found[f"0x{index:03X}"] = byte
        covered.add(index)
    return found


def _decode_playlist_prefs(data, body: int, length: int) -> dict[str, Any]:
    if length <= MHOD100_POSITION_BODY_SIZE:
        return {"position": struct.unpack_from("<I", data, body)[0]} if length >= 4 else {}
    raw = _slice(data, body, length)
    return {"fields": _nonzero_words(raw), "raw_body": raw}


def _decode_settings(data, body: int, length: int) -> dict[str, Any]:
    raw = _slice(data, body, length)
    return {"fields": _nonzero_words(raw), "raw_body": raw}


def _decode_property_plist(data, body: int, length: int) -> dict[str, Any]:
    return parse_playlist_property_mhod55(_slice(data, body, length))


_BINARY_BODIES: dict[int, Callable[[Any, int, int], dict[str, Any]]] = {
    50: _decode_prefs,
    51: decode_rule_list,
    52: _decode_sort_index,
    53: _decode_jump_table,
    55: _decode_property_plist,
    100: _decode_playlist_prefs,
    102: _decode_settings,
}


@handles("mhod")
def parse_data_object(data: bytes | bytearray, offset: int, header_length: int, chunk_length: int) -> ParseResult:
    """Decode one MHOD; the body is never counted as parsed for raw-span purposes."""
    fields = decode_fields(data, offset, "mhod", None)
    kind = fields["mhod_type"]
    body, length = offset + header_length, chunk_length - header_length
    if kind in STRING_MHOD_TYPES:
        fields.update(_decode_text(data, offset, length))
    elif kind in PODCAST_URL_MHOD_TYPES:
        fields["string"] = _decode_url(data, body, length)
    elif kind in CHAPTER_DATA_MHOD_TYPES:
        fields["data"] = _decode_chapters(data, body, length)
    elif kind in BINARY_BLOB_MHOD_TYPES:
        fields["string"] = _slice(data, body, length).hex()
    elif kind in NON_STRING_MHOD_TYPES:
        fields["data"] = _BINARY_BODIES.get(kind, lambda *_a: {})(data, body, length)
    else:
        fields["string"] = ""
    return {"next_offset": offset + chunk_length, "data": fields, "_body_end": body}
