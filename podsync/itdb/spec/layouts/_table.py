"""A tiny text format for header layouts.

Each non-blank line of a table is ``name  type  offset  [option ...]``:

* ``type`` — ``u8``, ``u16``, ``u32``, ``i32``, ``u64``, ``f32`` or ``bytesN`` (N raw bytes)
* ``offset`` — byte offset from the start of the chunk (hex or decimal)
* options:
  ``required`` · ``default=<python literal>`` · ``since=<length>`` (only present in
  headers at least that long) · ``mac-time`` (stored as device-local Mac seconds) ·
  ``via=<name>`` (a named converter from :data:`CONVERTERS`)

``# comments`` are ignored.  With ``extended_from=N``, every field at or after
offset N without an explicit ``since`` is only present when the header reaches
past the field's last byte.
"""

from __future__ import annotations

import ast
from typing import Any

from podsync.itdb.spec.fields import (
    FieldSpec,
    bounded_rating,
    check_rating,
    check_volume,
    f32,
    fixed16_to_hz,
    hz_to_fixed16,
    i32,
    mac_to_unix,
    raw_bytes,
    stamp_section,
    u8,
    u16,
    u32,
    u64,
    unix_to_mac,
)

_FACTORIES = {"u8": u8, "u16": u16, "u32": u32, "i32": i32, "u64": u64, "f32": f32}

CONVERTERS: dict[str, dict[str, Any]] = {
    "mac-time": {"read_transform": mac_to_unix, "write_transform": unix_to_mac},
    "rating": {"write_transform": bounded_rating, "validator": check_rating},
    "volume": {"validator": check_volume},
    "fixed16-hz": {"read_transform": fixed16_to_hz, "write_transform": hz_to_fixed16},
}


def _number(text: str) -> int:
    return int(text, 0)


def _parse_line(line: str, extended_from: int | None) -> FieldSpec:
    name, kind, offset_text, *options = line.split()
    offset = _number(offset_text)
    kwargs: dict[str, Any] = {}
    for option in options:
        key, _, value = option.partition("=")
        if key == "required":
            kwargs["required"] = True
        elif key == "default":
            kwargs["default"] = ast.literal_eval(value)
        elif key == "since":
            kwargs["min_header_length"] = _number(value)
        elif key == "mac-time":
            kwargs.update(CONVERTERS["mac-time"])
        elif key == "via":
            kwargs.update(CONVERTERS[value])
        else:
            raise ValueError(f"unknown layout option {option!r} in line {line!r}")
    if kind.startswith("bytes"):
        spec = raw_bytes(name, offset, int(kind[5:]), **kwargs)
    else:
        spec = _FACTORIES[kind](name, offset, **kwargs)
    if extended_from is not None and offset >= extended_from and "min_header_length" not in kwargs:
        spec = FieldSpec(**{**_as_dict(spec), "min_header_length": offset + spec.size})
    return spec


def _as_dict(spec: FieldSpec) -> dict[str, Any]:
    return {slot: getattr(spec, slot) for slot in FieldSpec.__slots__}


def record(tag: str, table: str, *, extended_from: int | None = None) -> list[FieldSpec]:
    """Parse a layout table into :class:`FieldSpec` objects stamped with *tag*."""
    specs = []
    for raw in table.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            specs.append(_parse_line(line, extended_from))
    return stamp_section(tag, specs)
