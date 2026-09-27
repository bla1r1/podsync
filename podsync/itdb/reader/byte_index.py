"""Browse huge byte-walk JSON documents without loading them whole.

A byte walk of a real database runs to tens of megabytes.  :func:`index_byte_walk_json`
memory-maps the file and records where each chunk object starts plus its
headline fields; chunks (or just their leaf fields, as an "outline") are then
decoded on demand, with a small thread-safe LRU cache in front.
"""

from __future__ import annotations

import json
import mmap
import sys
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

__all__ = [
    "ByteIndexCache",
    "ByteIndexEntry",
    "ByteWalkChunkLoad",
    "ByteWalkDocumentIndex",
    "build_chunk_hierarchy",
    "hex_interpretations",
    "index_byte_walk_document",
    "index_byte_walk_json",
    "load_indexed_chunk",
    "load_indexed_chunk_outline",
]

_CHUNK_KEY = b'"chunk": "'
_BYTES_KEY = b'"bytes": ['
_LOOKAHEAD = 16_384  # headline fields always sit this close to the chunk key
_WHITESPACE = b" \t\r\n"
_QUOTE, _BACKSLASH = 0x22, 0x5C


@dataclass(frozen=True, slots=True)
class ByteIndexEntry:
    json_offset: int  # where the chunk's JSON object starts
    chunk_type: str
    caption: str
    file_offset: int  # where the chunk starts in the database
    byte_length: int


@dataclass(frozen=True, slots=True)
class ByteWalkDocumentIndex:
    entries: list[ByteIndexEntry]
    children_by_parent: dict[int, tuple[ByteIndexEntry, ...]]


@dataclass(frozen=True, slots=True)
class ByteWalkChunkLoad:
    chunk: dict[str, Any]
    was_cached: bool


# ── scanning ────────────────────────────────────────────────────────


@contextmanager
def _mapped(path: str | Path) -> Iterator[bytes | mmap.mmap]:
    with open(path, "rb") as handle:
        if Path(path).stat().st_size == 0:
            yield b""
            return
        view = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            yield view
        finally:
            view.close()


class _Scanner:
    """Just enough JSON lexing to hop over values without decoding them."""

    def __init__(self, buf: bytes | mmap.mmap) -> None:
        self.buf = buf
        self.size = len(buf)

    def skip_space(self, pos: int) -> int:
        while pos < self.size and self.buf[pos] in _WHITESPACE:
            pos += 1
        return pos

    def string_end(self, start: int) -> int:
        """Index just past the closing quote of the string opening at *start*."""
        pos = start + 1
        while pos < self.size:
            char = self.buf[pos]
            if char == _BACKSLASH:
                pos += 2
            elif char == _QUOTE:
                return pos + 1
            else:
                pos += 1
        raise ValueError("unterminated JSON string in byte-walk document")

    def string(self, start: int) -> tuple[str, int]:
        end = self.string_end(start)
        return json.loads(bytes(self.buf[start:end]).decode("utf-8")), end

    def container_end(self, start: int) -> int | None:
        """End of the object/array opening at *start*, or ``None`` if it never closes."""
        depth = 0
        pos = start
        while pos < self.size:
            char = self.buf[pos]
            if char == _QUOTE:
                pos = self.string_end(pos)
                continue
            if char in b"{[":
                depth += 1
            elif char in b"}]":
                depth -= 1
                if depth == 0:
                    return pos + 1
            pos += 1
        return None

    def object_end(self, start: int) -> int:
        end = self.container_end(start)
        if end is None:
            raise ValueError("unterminated JSON object in byte-walk document")
        return end

    def value_end(self, start: int) -> int:
        pos = self.skip_space(start)
        if pos >= self.size:
            raise ValueError("unterminated JSON value in byte-walk document")
        opener = self.buf[pos]
        if opener == _QUOTE:
            return self.string_end(pos)
        if opener in b"{[":
            end = self.container_end(pos)
            if end is None:
                kind = "object" if opener == ord("{") else "value"
                raise ValueError(f"unterminated JSON {kind} in byte-walk document")
            return end
        while pos < self.size and self.buf[pos] not in b",}]" + _WHITESPACE:
            pos += 1
        return pos

    def after_key(self, start: int, limit: int, name: str) -> int:
        """Position of the value of ``"name": `` between *start* and *limit*."""
        key = f'"{name}": '.encode()
        found = self.buf.find(key, start, limit)
        if found < 0:
            raise ValueError(f"byte-walk chunk is missing {name!r}")
        return found + len(key)

    def holds_nested_chunk(self, start: int, end: int) -> bool:
        """Whether the object at *start* has a ``"chunk"`` key whose value is an object."""
        pos = start + 1
        while pos < end:
            pos = self.skip_space(pos)
            if self.buf[pos:pos + 1] != b'"':
                raise ValueError("byte-walk bytes array contains an invalid object key")
            key, pos = self.string(pos)
            pos = self.skip_space(pos)
            if self.buf[pos:pos + 1] != b":":
                raise ValueError("byte-walk bytes array contains an invalid object property")
            pos = self.skip_space(pos + 1)
            if key == "chunk":
                return self.buf[pos:pos + 1] == b"{"
            pos = self.skip_space(self.value_end(pos))
            separator = self.buf[pos:pos + 1]
            if separator == b"}":
                return False
            if separator != b",":
                raise ValueError("byte-walk bytes array contains an invalid object separator")
            pos += 1
        return False


# ── indexing ────────────────────────────────────────────────────────


def _index_entry(scan: _Scanner, key_at: int) -> tuple[ByteIndexEntry, int]:
    chunk_type, after = scan.string(key_at + len(_CHUNK_KEY) - 1)
    limit = min(scan.size, key_at + _LOOKAHEAD)
    caption, _ = scan.string(scan.after_key(after, limit, "caption"))
    offset_text, _ = scan.string(scan.after_key(after, limit, "file_offset"))
    length_at = scan.after_key(after, limit, "byte_length")
    digits_end = length_at
    while digits_end < limit and scan.buf[digits_end:digits_end + 1].isdigit():
        digits_end += 1
    digits = bytes(scan.buf[length_at:digits_end])
    if not digits:
        raise ValueError("byte-walk chunk has an invalid 'byte_length'")
    object_start = scan.buf.rfind(b"{", max(0, key_at - 256), key_at)
    if object_start < 0:
        raise ValueError("could not find the start of a byte-walk chunk")
    entry = ByteIndexEntry(object_start, chunk_type, caption, int(offset_text, 0), int(digits))
    return entry, after


def index_byte_walk_json(path: str | Path) -> list[ByteIndexEntry]:
    """Every chunk of a byte-walk document, in document order."""
    with _mapped(path) as buf:
        scan = _Scanner(buf)
        entries: list[ByteIndexEntry] = []
        cursor = 0
        while (key_at := buf.find(_CHUNK_KEY, cursor)) >= 0:
            entry, cursor = _index_entry(scan, key_at)
            entries.append(entry)
    if not entries:
        raise ValueError("this file does not contain a podsync byte-walk JSON document")
    return entries


def build_chunk_hierarchy(entries: list[ByteIndexEntry]) -> dict[int, tuple[ByteIndexEntry, ...]]:
    """Parent ``json_offset`` → direct children, derived from file-offset containment."""
    children: dict[int, list[ByteIndexEntry]] = {e.json_offset: [] for e in entries}
    open_chunks: list[ByteIndexEntry] = []
    for entry in sorted(entries, key=lambda e: (e.file_offset, -e.byte_length, e.json_offset)):
        end = entry.file_offset + entry.byte_length
        while open_chunks:
            parent = open_chunks[-1]
            if parent is not entry and parent.file_offset <= entry.file_offset and end <= parent.file_offset + parent.byte_length:
                break
            open_chunks.pop()
        if open_chunks:
            children[open_chunks[-1].json_offset].append(entry)
        open_chunks.append(entry)
    return {key: tuple(kids) for key, kids in children.items()}


def index_byte_walk_document(path: str | Path) -> ByteWalkDocumentIndex:
    entries = index_byte_walk_json(path)
    return ByteWalkDocumentIndex(entries=entries, children_by_parent=build_chunk_hierarchy(entries))


# ── loading ─────────────────────────────────────────────────────────


def load_indexed_chunk(path: str | Path, entry: ByteIndexEntry) -> dict[str, Any]:
    """Decode one chunk object in full (including all nested chunks)."""
    with _mapped(path) as buf:
        end = _Scanner(buf).object_end(entry.json_offset)
        parsed = json.loads(bytes(buf[entry.json_offset:end]).decode("utf-8"))
    if not isinstance(parsed, dict) or parsed.get("chunk") != entry.chunk_type:
        raise ValueError("byte-walk index does not match the selected chunk")
    return parsed


def _leaf_items(scan: _Scanner, entry: ByteIndexEntry, stop_at_children: bool) -> list[dict[str, Any]]:
    buf = scan.buf
    array_at = buf.find(_BYTES_KEY, entry.json_offset, min(scan.size, entry.json_offset + _LOOKAHEAD))
    if array_at < 0:
        raise ValueError("byte-walk chunk is missing its bytes array")
    pos = array_at + len(_BYTES_KEY)
    leaves: list[dict[str, Any]] = []
    while True:
        pos = scan.skip_space(pos)
        if pos >= scan.size:
            raise ValueError("unterminated byte-walk bytes array")
        char = buf[pos:pos + 1]
        if char == b"]":
            return leaves
        if char == b",":
            pos += 1
            continue
        if char != b"{":
            if char in (b'"', b"[") or char.isdigit():
                raise ValueError("byte-walk bytes array contains a non-object entry")
            raise ValueError("byte-walk bytes array contains an invalid entry")
        end = scan.object_end(pos)
        if stop_at_children and scan.holds_nested_chunk(pos, end):
            return leaves  # leaves always precede nested chunks we already know about
        item = json.loads(bytes(buf[pos:end]).decode("utf-8"))
        if not isinstance(item, dict):
            raise ValueError("byte-walk bytes array contains a non-object entry")
        leaves.append(item)
        pos = end


def load_indexed_chunk_outline(
    path: str | Path,
    entry: ByteIndexEntry,
    children: tuple[ByteIndexEntry, ...] | list[ByteIndexEntry],
) -> dict[str, Any]:
    """A chunk's own fields plus lightweight references to its (indexed) children."""
    with _mapped(path) as buf:
        leaves = _leaf_items(_Scanner(buf), entry, stop_at_children=bool(children))
    references = [
        {
            "at": f"0x{child.file_offset - entry.file_offset:X}",
            "byte_length": child.byte_length,
            "chunk": {
                "chunk": child.chunk_type,
                "caption": child.caption,
                "file_offset": f"0x{child.file_offset:X}",
                "byte_length": child.byte_length,
            },
            "indexed_chunk": child,
        }
        for child in children
    ]
    return {
        "chunk": entry.chunk_type,
        "caption": entry.caption,
        "file_offset": f"0x{entry.file_offset:X}",
        "byte_length": entry.byte_length,
        "bytes": [*leaves, *references],
    }


# ── cache ───────────────────────────────────────────────────────────


def _approximate_size(value: Any, seen: set[int] | None = None) -> int:
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    total = sys.getsizeof(value)
    if isinstance(value, dict):
        total += sum(_approximate_size(k, seen) + _approximate_size(v, seen) for k, v in value.items())
    elif isinstance(value, (list, tuple, set, frozenset)):
        total += sum(_approximate_size(item, seen) for item in value)
    return total


class _Pending:
    """A load another thread is already performing."""

    __slots__ = ("done", "value", "error", "generation")

    def __init__(self, generation: int) -> None:
        self.done = threading.Event()
        self.value: dict[str, Any] | None = None
        self.error: BaseException | None = None
        self.generation = generation


class ByteIndexCache:
    """LRU cache of decoded chunks, bounded by entry count and approximate memory.

    Concurrent requests for the same chunk share one decode.  ``clear()`` also
    discards results of loads that were in flight when it was called.
    """

    def __init__(self, *, max_entries: int = 16, max_bytes: int = 16 * 1024 * 1024) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._max_entries, self._max_bytes = max_entries, max_bytes
        self._lock = threading.Lock()
        self._items: OrderedDict[tuple, tuple[dict[str, Any], int]] = OrderedDict()
        self._pending: dict[tuple, _Pending] = {}
        self._bytes = 0
        self._generation = 0

    def load(self, path: str | Path, entry: ByteIndexEntry) -> ByteWalkChunkLoad:
        # load_indexed_chunk is looked up at call time so it can be substituted.
        return self._get(("full", Path(path).resolve(), entry.json_offset), lambda: load_indexed_chunk(path, entry))

    def load_outline(
        self, path: str | Path, entry: ByteIndexEntry,
        children: tuple[ByteIndexEntry, ...] | list[ByteIndexEntry],
    ) -> ByteWalkChunkLoad:
        return self._get(
            ("outline", Path(path).resolve(), entry.json_offset),
            lambda: load_indexed_chunk_outline(path, entry, children),
        )

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._pending.clear()
            self._bytes = 0
            self._generation += 1

    def _get(self, key: tuple, produce: Callable[[], dict[str, Any]]) -> ByteWalkChunkLoad:
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return ByteWalkChunkLoad(chunk=self._items[key][0], was_cached=True)
            pending = self._pending.get(key)
            owner = pending is None
            if owner:
                pending = self._pending[key] = _Pending(self._generation)

        if not owner:
            pending.done.wait()
            if pending.error is not None:
                raise pending.error
            if pending.value is None:
                raise RuntimeError("chunk load completed without a result")
            return ByteWalkChunkLoad(chunk=pending.value, was_cached=True)

        try:
            pending.value = produce()
        except BaseException as exc:
            pending.error = exc
            raise
        else:
            with self._lock:
                if pending.generation == self._generation:
                    self._remember(key, pending.value)
            return ByteWalkChunkLoad(chunk=pending.value, was_cached=False)
        finally:
            with self._lock:
                if self._pending.get(key) is pending:
                    del self._pending[key]
            pending.done.set()

    def _remember(self, key: tuple, value: dict[str, Any]) -> None:
        size = _approximate_size(value)
        if size > self._max_bytes:
            return
        previous = self._items.pop(key, None)
        if previous is not None:
            self._bytes -= previous[1]
        self._items[key] = (value, size)
        self._bytes += size
        while len(self._items) > self._max_entries or self._bytes > self._max_bytes:
            _key, (_value, evicted) = self._items.popitem(last=False)
            self._bytes -= evicted


# ── hex helper ──────────────────────────────────────────────────────


def hex_interpretations(hex_text: str) -> dict[str, str]:
    """Every sensible reading of a short hex selection (text encodings, integers, Mac time)."""
    cleaned = hex_text.lower()
    for noise in ("0x", " ", "\n", "\t", ":", ","):
        cleaned = cleaned.replace(noise, "")
    if not cleaned:
        return {}
    if len(cleaned) % 2:
        raise ValueError("hex input has an odd number of digits")
    if cleaned.strip("0123456789abcdef"):
        raise ValueError("hex input contains a non-hexadecimal character")
    raw = bytes.fromhex(cleaned)
    even = len(raw) % 2 == 0
    readings: dict[str, str] = {
        "Byte count": str(len(raw)),
        "ASCII": raw.decode("ascii", errors="replace"),
        "UTF-8": raw.decode("utf-8", errors="replace"),
        "UTF-16 LE": raw.decode("utf-16-le", errors="replace") if even else "— (odd byte count)",
        "UTF-16 BE": raw.decode("utf-16-be", errors="replace") if even else "— (odd byte count)",
    }
    if len(raw) in (1, 2, 4, 8):
        bits = len(raw) * 8
        orders = ("little", "big") if len(raw) > 1 else ("little",)
        for order in orders:
            label = "LE" if order == "little" else "BE"
            readings[f"Unsigned {bits}-bit {label}"] = str(int.from_bytes(raw, order))
            readings[f"Signed {bits}-bit {label}"] = str(int.from_bytes(raw, order, signed=True))
    if len(raw) == 4:
        moment = datetime(1904, 1, 1, tzinfo=UTC) + timedelta(seconds=int.from_bytes(raw, "little"))
        readings["Mac epoch timestamp (u32 LE)"] = moment.isoformat()
    return readings
