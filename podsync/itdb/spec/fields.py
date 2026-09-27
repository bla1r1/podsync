"""Declarative binary layouts and the codec that reads/writes them.

Each record kind (``mhit``, ``mhyp``, ...) registers a list of :class:`FieldSpec`
in :data:`LAYOUTS` (see :mod:`podsync.itdb.spec.layouts`).  A spec knows its
offset, ``struct`` format, default, optional read/write transforms and the
minimum header length that carries it; older, shorter headers simply omit it.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any

from podsync.itdb.spec.clock import MAC_EPOCH_OFFSET as MAC_EPOCH_OFFSET
from podsync.itdb.spec.clock import MAC_U32_MAX, active_device_clock

# Every chunk starts with: 4-byte tag, u32 header length, u32 total length (or child count).
CHUNK_HEADER_FORMAT = "<4sII"
CHUNK_HEADER_SIZE = struct.calcsize(CHUNK_HEADER_FORMAT)

# The list chunks (tracks, albums, artists, playlists) all use a 92-byte header.
MHLT_HEADER_SIZE = MHLA_HEADER_SIZE = MHLI_HEADER_SIZE = MHLP_HEADER_SIZE = 92

U32_MAX = MAC_U32_MAX


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One fixed-position field inside a chunk header."""

    name: str
    offset: int
    size: int
    struct_format: str
    read_transform: Callable[[Any], Any] | None = None
    write_transform: Callable[[Any], Any] | None = None
    default: Any = 0
    validator: Callable[[Any], Any] | None = None
    min_header_length: int | None = None
    required: bool = False
    section_type: str = ""

    def present_in(self, header_length: int | None) -> bool:
        """Whether a header of *header_length* bytes carries this field."""
        if self.min_header_length is None:
            return True
        return header_length is not None and header_length >= self.min_header_length


def _spec(fmt: str, size: int, default: Any = 0) -> Callable[..., FieldSpec]:
    def build(name: str, offset: int, **options: Any) -> FieldSpec:
        options.setdefault("default", default)
        return FieldSpec(name, offset, size, fmt, **options)
    build.__name__ = f"field_{fmt.strip('<')}"
    return build


u8 = _spec("B", 1)
u16 = _spec("<H", 2)
u32 = _spec("<I", 4)
i32 = _spec("<i", 4)
u64 = _spec("<Q", 8)
f32 = _spec("<f", 4, default=0.0)


def raw_bytes(name: str, offset: int, size: int, **options: Any) -> FieldSpec:
    """An opaque byte run kept verbatim (hashes, reserved areas)."""
    options.setdefault("default", bytes(size))
    return FieldSpec(name, offset, size, f"<{size}s", **options)


def stamp_section(section_type: str, fields: list[FieldSpec]) -> list[FieldSpec]:
    """Tag each field with its chunk kind so write errors can name it."""
    return [replace(spec, section_type=section_type) for spec in fields]


# ── errors ──────────────────────────────────────────────────────────


class FieldWriteError(Exception):
    """A header could not be encoded."""


class MissingFieldError(FieldWriteError):
    """A field required by the layout is absent from the record being encoded."""

    def __init__(self, section_type: str, field_name: str) -> None:
        self.section_type = section_type
        self.field_name = field_name
        super().__init__(f"Required field '{field_name}' missing for section '{section_type}'")


class BadFieldValueError(FieldWriteError):
    """A record value cannot be encoded in its field's width or range."""

    def __init__(self, section_type: str, field_name: str, detail: str) -> None:
        self.section_type = section_type
        self.field_name = field_name
        super().__init__(f"Invalid value for '{field_name}' in section '{section_type}': {detail}")


# ── value conversions ───────────────────────────────────────────────


def _to_int(value: Any, default: int = 0) -> int:
    if isinstance(value, float) and not math.isfinite(value):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def mac_to_unix(mac_ts: int) -> int:
    """Device-local Mac timestamp → Unix time, using the active device clock."""
    return active_device_clock().mac_to_unix(mac_ts)


def unix_to_mac(unix_ts: int) -> int:
    """Unix time → device-local Mac timestamp, using the active device clock."""
    return active_device_clock().unix_to_mac(_to_int(unix_ts))


def hz_to_fixed16(hz: Any) -> int:
    """Sample rate in Hz → the 16.16 fixed-point value MHIT stores (clamped to 16 bits)."""
    return min(max(_to_int(hz), 0), 0xFFFF) << 16


def fixed16_to_hz(raw: int) -> int:
    """Sample rate in Hz from a 16.16 fixed-point value."""
    return raw >> 16


def check_rating(value: Any) -> None:
    """Raise ``ValueError`` unless *value* is a rating between 0 and 100."""
    if not 0 <= _to_int(value, -1) <= 100:
        raise ValueError(f"rating {value} outside 0-100")


def bounded_rating(value: Any) -> int:
    """*value* clamped to the 0–100 rating range."""
    return min(max(_to_int(value), 0), 100)


def check_volume(value: Any) -> None:
    """Raise ``ValueError`` unless *value* is a volume adjustment between -255 and +255."""
    if not -255 <= _to_int(value, -1000) <= 255:
        raise ValueError(f"volume {value} outside -255..+255")


def fourcc_text(val: Any) -> str:
    """A stored four-character code (``0x4D503320``) as text (``"MP3"``); ``""`` when unset."""
    if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
        return ""
    try:
        return val.to_bytes(4, "big").decode("ascii").rstrip("\x00").strip()
    except (OverflowError, UnicodeDecodeError):
        return str(val)


_ARTICLES = ("the ", "a ", "an ")


def without_article(name: str) -> str:
    """Drop a leading English article for sorting ("The Beatles" → "Beatles")."""
    lowered = (name or "").lower()
    for article in _ARTICLES:
        if lowered.startswith(article):
            return name[len(article):]
    return name


# ── layout registry and codec ───────────────────────────────────────

LAYOUTS: dict[str, list[FieldSpec]] = {}


def layout_for(section_type: str, header_length: int | None = None) -> list[FieldSpec]:
    """The registered fields of a chunk kind, optionally limited to a header length."""
    specs = LAYOUTS.get(section_type, [])
    if header_length is None:
        return list(specs)
    return [spec for spec in specs if spec.present_in(header_length)]


def decode_field(
    data: bytes | bytearray | memoryview,
    base_offset: int,
    field: FieldSpec,
    header_length: int | None = None,
) -> Any:
    """One field of a chunk header, or ``None`` when it lies beyond *header_length*."""
    if not field.present_in(header_length):
        return field.default
    (raw,) = struct.unpack_from(field.struct_format, data, base_offset + field.offset)
    return field.read_transform(raw) if field.read_transform else raw


def decode_fields(
    data: bytes | bytearray | memoryview,
    base_offset: int,
    section_type: str,
    header_length: int | None = None,
) -> dict[str, Any]:
    """Every field of *section_type*'s layout that fits inside the header."""
    return {
        spec.name: decode_field(data, base_offset, spec, header_length)
        for spec in LAYOUTS.get(section_type, [])
    }


def _int_bounds(code: str) -> tuple[int, int] | None:
    """Representable range of an integer struct code, or ``None`` for non-integers."""
    if code not in "BbHhIiQq":
        return None
    bits = struct.calcsize(code) * 8
    if code.islower():
        return -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return 0, (1 << bits) - 1


def encode_field(
    buffer: bytearray,
    base_offset: int,
    field: FieldSpec,
    value: Any,
    section_type: str = "",
) -> None:
    """Pack one value.  Integers are clamped to the field's range rather than wrapped."""
    if field.write_transform is not None:
        value = field.write_transform(value)
    if field.validator is not None:
        try:
            field.validator(value)
        except (ValueError, TypeError) as exc:
            raise BadFieldValueError(section_type or field.section_type, field.name, str(exc)) from exc
    code = field.struct_format[-1]
    bounds = _int_bounds(code)
    if bounds is not None or code in "NP":
        value = value if isinstance(value, int) else int(value)
        if bounds is not None:
            value = min(max(value, bounds[0]), bounds[1])
    elif code in "fd" and not isinstance(value, float):
        value = float(value)
    try:
        struct.pack_into(field.struct_format, buffer, base_offset + field.offset, value)
    except struct.error as exc:
        raise struct.error(
            f"Failed to pack field '{field.name}' (format={field.struct_format!r}, "
            f"value={value!r} type={type(value).__name__}): {exc}"
        ) from exc


def encode_fields(
    buffer: bytearray,
    base_offset: int,
    section_type: str,
    values: dict[str, Any],
    header_length: int,
) -> None:
    """Pack every registered field that fits in *header_length*; defaults fill the gaps."""
    for spec in LAYOUTS.get(section_type, []):
        if not spec.present_in(header_length):
            continue
        if spec.name in values:
            value = values[spec.name]
        elif spec.required:
            raise MissingFieldError(section_type, spec.name)
        else:
            value = spec.default
        encode_field(buffer, base_offset, spec, value, section_type)


# ── chunk and list headers ──────────────────────────────────────────


def pack_chunk_header(
    buffer: bytearray, offset: int, tag: bytes, header_length: int, total_length_or_count: int,
) -> None:
    """Write the 12-byte ``tag, header length, total/count`` prefix into *buffer*."""
    struct.pack_into(CHUNK_HEADER_FORMAT, buffer, offset, tag, header_length, total_length_or_count)


def list_header_bytes(tag: bytes, header_length: int, child_count: int) -> bytes:
    """A zero-padded list header whose third word is the child count."""
    header = bytearray(header_length)
    pack_chunk_header(header, 0, tag, header_length, child_count)
    return bytes(header)


def list_chunk_bytes(tag: bytes, header_length: int, child_chunks: Iterable[bytes]) -> bytes:
    """A list chunk header followed by its children (the count is the child count)."""
    children = list(child_chunks)
    return list_header_bytes(tag, header_length, len(children)) + b"".join(children)
