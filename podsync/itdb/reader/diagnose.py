"""A lossless, human-readable "byte walk" of an iTunesDB as JSON.

Every chunk becomes ``{"chunk", "caption", "file_offset", "byte_length", "bytes"}``
where ``bytes`` is an ordered list that tiles the chunk exactly: decoded fields,
padding, nested chunks, and anything not understood (``"status": "unmapped"``).
Because each entry carries its hex, :func:`reconstruct_byte_walk` rebuilds the
original file byte for byte — the export refuses to finish if it cannot.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

from podsync.itdb.reader.entry import decompress_itunescdb, read_itdb
from podsync.itdb.spec.codes import CHUNK_LABELS, MHOD_FIELD_KEYS
from podsync.itdb.spec.fields import LAYOUTS
from podsync.itdb.spec.flatten import collect_strings
from podsync.itdb.spec.layouts.strings import (
    BINARY_BLOB_MHOD_TYPES,
    CHAPTER_DATA_MHOD_TYPES,
    MHOD52_BODY_HEADER_SIZE,
    MHOD53_BODY_HEADER_SIZE,
    MHOD53_ENTRY_SIZE,
    MHOD100_POSITION_BODY_SIZE,
    PODCAST_URL_MHOD_TYPES,
    STRING_MHOD_TYPES,
)
from podsync.itdb.spec.smart_fields import (
    SLST_HEADER_SIZE,
    SPL_GROUP_MARKER,
    SPL_RULE_DATA_SIZE,
    SPL_RULE_HEADER_SIZE,
    SPLFT_STRING,
    SPLFT_UNKNOWN,
    spl_get_field_type,
)

__all__ = ["FORMAT_ID", "export_forensic_json", "forensic_json_document", "reconstruct_byte_walk"]

FORMAT_ID = "podsync-byte-walk/v1"

_PHASE_MUSIC_NOTE = (
    "Observed value 25 (0x0019) occurs in both mirrored Phase Music playlists; "
    "its purpose is not yet proven."
)
_LIST_TAGS = frozenset({"mhlt", "mhla", "mhli", "mhlp"})


def _at(value: int) -> str:
    return f"0x{value:04X}"


def _hex(value: bytes) -> str:
    return bytes(value).hex(" ")


def _jsonable(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return {"hex": _hex(value), "byte_length": len(value)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _default_status(name: str) -> str:
    """Fields whose meaning is only observed, not proven, are flagged as such."""
    return "observed" if name.startswith(("unk", "mhsd5")) or name == "phase_game_flag" else "known"


class _SpanMap:
    """Annotations of one chunk, in chunk-local byte offsets."""

    def __init__(self, data: bytes, base: int, length: int) -> None:
        self.data, self.base, self.length = data, base, length
        self.spans: list[tuple[int, int, dict[str, Any]]] = []

    def _inside(self, start: int, size: int) -> bool:
        return size > 0 and start >= 0 and start + size <= self.length

    def bytes_at(self, start: int, size: int) -> bytes:
        return bytes(self.data[self.base + start:self.base + start + size])

    def field(self, start: int, size: int, name: str, value: Any, *,
              status: str | None = None, encoding: str | None = None, note: str | None = None) -> None:
        if not self._inside(start, size):
            return
        entry: dict[str, Any] = {"field": name, "value": _jsonable(value)}
        if encoding is not None:
            entry["encoding"] = encoding
        entry["status"] = status or _default_status(name)
        if note is not None:
            entry["note"] = note
        self.spans.append((start, start + size, entry))

    def number(self, start: int, fmt: str, name: str, encoding: str, *, status: str | None = None) -> int | None:
        size = struct.calcsize(fmt)
        if not self._inside(start, size):
            return None
        (value,) = struct.unpack(fmt, self.bytes_at(start, size))
        self.field(start, size, name, value, status=status, encoding=encoding)
        return value

    def region(self, start: int, end: int, *, name: str | None = None, status: str) -> None:
        end = min(end, self.length)
        if end > start >= 0:
            self.spans.append((start, end, {"field": name, "status": status} if name else {"status": status}))

    def padding(self, start: int, end: int) -> None:
        self.region(start, end, status="padding")

    def nest(self, start: int, end: int, chunk: dict[str, Any]) -> None:
        self.spans.append((start, end, {"chunk": chunk}))

    def render(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = 0
        for start, end, detail in sorted(self.spans, key=lambda span: (span[0], span[1])):
            if start < 0 or end > self.length:
                raise ValueError("byte-walk annotation falls outside its chunk")
            if start < cursor:
                raise ValueError("byte-walk annotations overlap")
            self._unmapped(out, cursor, start)
            entry: dict[str, Any] = {"at": _at(start), "byte_length": end - start}
            if "chunk" in detail:
                entry["chunk"] = detail["chunk"]
            else:
                entry.update({k: detail[k] for k in ("field", "value", "encoding") if k in detail})
                entry["hex"] = _hex(self.bytes_at(start, end - start))
                entry["status"] = detail["status"]
                if "note" in detail:
                    entry["note"] = detail["note"]
            out.append(entry)
            cursor = end
        self._unmapped(out, cursor, self.length)
        return out

    def _unmapped(self, out: list[dict[str, Any]], start: int, end: int) -> None:
        if end > start:
            out.append({
                "at": _at(start), "byte_length": end - start,
                "hex": _hex(self.bytes_at(start, end - start)), "status": "unmapped",
            })


# ── MHOD bodies ─────────────────────────────────────────────────────


def _string_body(m: _SpanMap, body: int, end: int) -> None:
    if end - body < 16:
        return
    encoding = m.number(body, "<I", "string_encoding", "u32le")
    byte_length = m.number(body + 4, "<I", "string_byte_length", "u32le") or 0
    for extra, name in ((8, "unk_0x20"), (12, "unk_0x24")):
        (word,) = struct.unpack("<I", m.bytes_at(body + extra, 4))
        m.field(body + extra, 4, name, word, status="observed")
    text_start, text_end = body + 16, min(body + 16 + byte_length, end)
    if text_end > text_start:
        utf8 = encoding == 2
        raw = m.bytes_at(text_start, text_end - text_start)
        m.field(text_start, text_end - text_start, "text", raw.decode("utf-8" if utf8 else "utf-16-le", errors="replace"),
                status="known", encoding="utf-8" if utf8 else "utf-16le")


def _url_body(m: _SpanMap, body: int, end: int) -> None:
    raw = m.bytes_at(body, end - body)
    m.field(body, end - body, "text", raw.decode("utf-8", errors="replace"), status="known", encoding="utf-8")


def _prefs_body(m: _SpanMap, body: int, end: int) -> None:
    if end - body < 12:
        return
    for index, name in enumerate(("live_update", "check_rules", "check_limits", "limit_type", "limit_sort")):
        m.number(body + index, "B", name, "u8")
    m.padding(body + 5, body + 8)
    m.number(body + 8, "<I", "limit_value", "u32le")
    m.number(body + 12, "B", "match_checked_only", "u8")
    m.number(body + 13, "B", "reverse_sort", "u8")


def _rules_body(m: _SpanMap, body: int, end: int) -> None:
    if end - body >= SLST_HEADER_SIZE and m.bytes_at(body, 4) == b"SLst":
        _slst(m, body, end)
    else:
        m.region(body, end, name="opaque_payload", status="opaque")


def _slst(m: _SpanMap, body: int, end: int) -> None:
    m.field(body, 4, "slst_magic", "SLst", status="known", encoding="ascii")
    m.number(body + 4, ">I", "unk004", "u32be")
    count = m.number(body + 8, ">I", "rule_count", "u32be") or 0
    m.number(body + 12, ">I", "conjunction", "u32be")
    m.padding(body + 16, body + SLST_HEADER_SIZE)
    pos = body + SLST_HEADER_SIZE
    for _ in range(count):
        if end - pos < SPL_RULE_HEADER_SIZE:
            break
        pos = _rule(m, pos, end)


# Numeric rule payload: (name, struct format, JSON encoding label).
_RULE_NUMBERS = (
    ("from_value", ">Q", "u64be"), ("from_date", ">q", "u64be"), ("from_units", ">Q", "u64be"),
    ("to_value", ">Q", "u64be"), ("to_date", ">q", "u64be"), ("to_units", ">Q", "u64be"),
    ("unk052", ">I", "u32be"), ("unk056", ">I", "u32be"), ("unk060", ">I", "u32be"),
    ("unk064", ">I", "u32be"), ("unk068", ">I", "u32be"),
)


def _rule(m: _SpanMap, pos: int, end: int) -> int:
    field_id, action_id, marker = struct.unpack(">III", m.bytes_at(pos, 12))
    (data_length,) = struct.unpack(">I", m.bytes_at(pos + 0x34, 4))
    payload = pos + SPL_RULE_HEADER_SIZE
    have = max(0, min(data_length, end - payload))
    grouped = (
        field_id == 0 and action_id == 1 and marker == SPL_GROUP_MARKER
        and data_length >= SLST_HEADER_SIZE and have == data_length and m.bytes_at(payload, 4) == b"SLst"
    )
    m.number(pos, ">I", "rule_field_id", "u32be")
    m.number(pos + 4, ">I", "rule_action_id", "u32be")
    if grouped:
        m.number(pos + 8, ">I", "group_marker", "u32be")
        m.field(pos + 12, 40, "group_header_bytes", m.bytes_at(pos + 12, 40), status="known")
    else:
        m.padding(pos + 8, pos + 52)
    m.number(pos + 52, ">I", "rule_data_length", "u32be")

    kind = spl_get_field_type(field_id)
    if grouped:
        _slst(m, payload, payload + data_length)
    elif kind == SPLFT_STRING or (kind == SPLFT_UNKNOWN and (data_length == 0 or action_id & 0x01000000)):
        if have:
            m.field(payload, have, "rule_text", m.bytes_at(payload, have).decode("utf-16-be", errors="replace"),
                    status="known", encoding="utf-16be")
    elif have < SPL_RULE_DATA_SIZE:
        m.region(payload, payload + have, name="rule_data", status="partially_decoded")
    else:
        cursor = payload
        for name, fmt, encoding in _RULE_NUMBERS:
            m.number(cursor, fmt, name, encoding)
            cursor += struct.calcsize(fmt)
    return payload + data_length


def _index_body(m: _SpanMap, body: int, end: int) -> None:
    if end - body < 8:
        return
    m.number(body, "<I", "sort_type", "u32le")
    count = m.number(body + 4, "<I", "count", "u32le") or 0
    m.padding(body + 8, body + MHOD52_BODY_HEADER_SIZE)
    pos = body + MHOD52_BODY_HEADER_SIZE
    for _ in range(count):
        if pos + 4 > end:
            break
        m.number(pos, "<I", "track_index", "u32le")
        pos += 4


def _jump_table_body(m: _SpanMap, body: int, end: int) -> None:
    if end - body < 8:
        return
    m.number(body, "<I", "sort_type", "u32le")
    count = m.number(body + 4, "<I", "count", "u32le") or 0
    m.padding(body + 8, body + MHOD53_BODY_HEADER_SIZE)
    pos = body + MHOD53_BODY_HEADER_SIZE
    for _ in range(count):
        if pos + MHOD53_ENTRY_SIZE > end:
            break
        letter, _pad, start, run = struct.unpack("<HHII", m.bytes_at(pos, MHOD53_ENTRY_SIZE))
        m.field(pos, MHOD53_ENTRY_SIZE, "jump_table_entry",
                {"letter_code": letter, "start": start, "count": run}, status="known")
        pos += MHOD53_ENTRY_SIZE


def _sparse_body(m: _SpanMap, body: int, end: int) -> None:
    raw = m.bytes_at(body, end - body)
    covered: set[int] = set()
    for index, byte in enumerate(raw):
        if byte == 0 or index in covered:
            continue
        aligned = index & ~3
        if aligned + 4 <= len(raw):
            (word,) = struct.unpack_from("<I", raw, aligned)
            if word:
                m.field(body + aligned, 4, "observed_nonzero_u32", word, status="observed", encoding="u32le")
                covered.update(range(aligned, aligned + 4))
                continue
        m.field(body + index, 1, "observed_nonzero_u32", byte, status="observed", encoding="u8")
        covered.add(index)


def _position_or_sparse(m: _SpanMap, body: int, end: int) -> None:
    if end - body > MHOD100_POSITION_BODY_SIZE:
        _sparse_body(m, body, end)
    elif end - body >= 4:
        m.number(body, "<I", "position", "u32le")
        m.padding(body + 4, end)


_Annotator = Callable[[_SpanMap, int, int], None]
_BODY_ANNOTATORS: dict[int, _Annotator] = {
    50: _prefs_body,
    51: _rules_body,
    52: _index_body,
    53: _jump_table_body,
    100: _position_or_sparse,
    102: _sparse_body,
}


def _opaque(m: _SpanMap, body: int, end: int) -> None:
    m.region(body, end, name="opaque_payload", status="opaque")


def _annotator_for(kind: Any) -> _Annotator:
    if kind in STRING_MHOD_TYPES:
        return _string_body
    if kind in PODCAST_URL_MHOD_TYPES:
        return _url_body
    if kind in CHAPTER_DATA_MHOD_TYPES:
        return lambda m, body, end: m.region(body, end, name="chapter_atom_tree", status="partially_decoded")
    if kind in BINARY_BLOB_MHOD_TYPES or kind == 55:
        return _opaque
    return _BODY_ANNOTATORS.get(
        kind, lambda m, body, end: m.region(body, end, name="unclassified_mhod_payload", status="opaque"),
    )


# ── chunks ──────────────────────────────────────────────────────────


def _nested_wrappers(payload: Any) -> list[dict[str, Any]]:
    """Child wrappers carrying raw-span info, deduplicated and in file order."""
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        candidates = [
            *(payload.get("children") or []),
            *(payload.get("mhod_children") or []),
            *(payload.get("mhip_children") or []),
        ]
    else:
        candidates = []
    by_offset: dict[int, dict[str, Any]] = {}
    for wrapper in candidates:
        if isinstance(wrapper, dict) and "_raw_chunk" in wrapper:
            by_offset.setdefault(wrapper["_raw_chunk"]["offset"], wrapper)
    return [by_offset[offset] for offset in sorted(by_offset)]


def _playlist_title(payload: Any) -> str | None:
    return collect_strings(payload.get("mhod_children") or []).get("title") if isinstance(payload, dict) else None


def _caption(tag: str, payload: Any) -> str:
    if tag == "mhyp":
        title = _playlist_title(payload)
        if title:
            return f"Playlist: {title}"
    if tag == "mhod" and isinstance(payload, dict):
        kind = payload.get("mhod_type")
        name = MHOD_FIELD_KEYS.get(kind)
        return f"Data Object: {name}" if name else f"Data Object: type {kind}"
    return CHUNK_LABELS.get(tag, tag)


def _chunk_document(data: bytes, payload: Any, raw: dict[str, Any]) -> dict[str, Any]:
    start, end = raw["offset"], raw["end_offset"]
    length = end - start
    header_bytes = len(raw["raw_header"])
    tag = bytes(raw["raw_header"][:4]).decode("ascii", errors="replace")
    m = _SpanMap(data, start, length)

    if header_bytes >= 4:
        m.field(0, 4, "chunk_type", tag, status="known", encoding="ascii")
    if header_bytes >= 8:
        m.number(4, "<I", "header_length", "u32le", status="known")
    if header_bytes >= 12:
        m.number(8, "<I", "declared_length_or_child_count", "u32le", status="known")

    phase_music = tag == "mhyp" and _playlist_title(payload) == "Phase Music"
    for spec in LAYOUTS.get(tag, []):
        if spec.offset < 12 or spec.offset + spec.size > min(raw["header_length"], header_bytes):
            continue
        (value,) = struct.unpack(spec.struct_format, m.bytes_at(spec.offset, spec.size))
        note = _PHASE_MUSIC_NOTE if phase_music and spec.name == "phase_game_flag" else None
        m.field(spec.offset, spec.size, spec.name, value, note=note)

    for wrapper in _nested_wrappers(payload):
        child_raw = wrapper["_raw_chunk"]
        m.nest(child_raw["offset"] - start, child_raw["end_offset"] - start,
               _chunk_document(data, wrapper.get("data"), child_raw))

    if tag == "mhod" and isinstance(payload, dict):
        if length > header_bytes:
            _annotator_for(payload.get("mhod_type"))(m, header_bytes, length)
    elif tag == "mhsd" and isinstance(payload, dict) and "genius_cuid" in payload:
        if length > header_bytes:
            m.field(header_bytes, length - header_bytes, "genius_cuid", payload.get("genius_cuid", ""),
                    status="known", encoding="ascii")
    elif tag not in LAYOUTS and tag not in _LIST_TAGS:
        m.region(header_bytes, length, name="opaque_payload", status="opaque")

    return {
        "chunk": tag,
        "caption": _caption(tag, payload),
        "file_offset": _at(start),
        "byte_length": length,
        "bytes": m.render(),
    }


# ── public API ──────────────────────────────────────────────────────


def forensic_json_document(source: str | Path) -> dict[str, Any]:
    """Build the byte-walk document and prove it reproduces the file exactly."""
    path = Path(source)
    original = path.read_bytes()
    parsed = read_itdb(path, preserve_raw=True)
    root = parsed.get("_raw_chunk") if isinstance(parsed, dict) else None
    if root is None:
        raise ValueError("byte-walk reconstruction does not match the source file")
    document = {
        "format": FORMAT_ID,
        "source": {
            "filename": path.name,
            "byte_length": len(original),
            "sha256": hashlib.sha256(original).hexdigest(),
        },
        "file": _chunk_document(bytes(decompress_itunescdb(original)), parsed, root),
    }
    if reconstruct_byte_walk(document) != original:
        raise ValueError("byte-walk reconstruction does not match the source file")
    return document


def export_forensic_json(source: str | Path, destination: str | Path) -> Path:
    output = Path(destination)
    output.write_text(json.dumps(forensic_json_document(source), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return output


def _rebuild(chunk: dict[str, Any]) -> bytes:
    pieces: list[bytes] = []
    cursor = 0
    for entry in chunk["bytes"]:
        if int(entry["at"], 16) != cursor:
            raise ValueError(f"byte walk for {chunk['chunk']} skips or overlaps at 0x{cursor:04X}")
        piece = _rebuild(entry["chunk"]) if isinstance(entry.get("chunk"), dict) else bytes.fromhex(entry.get("hex", ""))
        if len(piece) != entry["byte_length"]:
            raise ValueError("byte-walk entry length does not match its bytes")
        pieces.append(piece)
        cursor += len(piece)
    if cursor != chunk["byte_length"]:
        raise ValueError(
            f"byte walk for {chunk['chunk']} ends at 0x{cursor:04X}, not 0x{chunk['byte_length']:04X}"
        )
    return b"".join(pieces)


def reconstruct_byte_walk(document: dict[str, Any]) -> bytes:
    """The exact bytes a byte-walk document describes."""
    return _rebuild(document["file"])
