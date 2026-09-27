"""One-line renderings of device identity for log messages.

``format_fields(device_dict)`` → ``model=MA446, family=iPod, pid=0x1209, …``;
empty values, zeros and (by default) false flags are left out, long values
are shortened in the middle, byte strings become ``<n bytes>`` and format
maps become ``count[ids]`` so a log line stays readable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

IDENTITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("model_number", "model"), ("model_family", "family"), ("generation", "gen"),
    ("capacity", "capacity"), ("color", "color"), ("serial", "serial"),
    ("firewire_guid", "fwguid"), ("firmware", "fw"), ("usb_vid", "vid"), ("usb_pid", "pid"),
    ("usb_serial", "usb_serial"), ("scsi_vendor", "scsi_vendor"),
    ("scsi_product", "scsi_product"), ("scsi_revision", "scsi_rev"),
)
CAPABILITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("family_id", "family_id"), ("updater_family_id", "updater_id"),
    ("product_type", "product"), ("db_version", "db_version"),
    ("shadow_db_version", "shadow_db"), ("uses_sqlite_db", "sqlite"),
    ("supports_sparse_artwork", "sparse_art"), ("max_tracks", "max_tracks"),
    ("max_file_size_gb", "max_file_gb"), ("max_transfer_speed", "max_transfer"),
    ("podcasts_supported", "podcasts"), ("voice_memos_supported", "voice_memos"),
    ("artwork_formats", "art_ids"), ("photo_formats", "photo_ids"),
    ("chapter_image_formats", "chapter_ids"),
)
SOURCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("serial", "serial"), ("firewire_guid", "fwguid"), ("model_number", "model"),
    ("model_family", "family"), ("generation", "gen"), ("capacity", "capacity"),
    ("color", "color"), ("usb_pid", "pid"), ("firmware", "fw"),
    ("filesystem_type", "filesystem"),
)
_FORMAT_FIELDS = {"artwork_formats", "photo_formats", "chapter_image_formats"}


def is_missing(value: Any) -> bool:
    """Whether *value* counts as absent in a log line."""
    return value is None or value == "" or value == b"" or value == {} or value == []


def compact(value: Any, *, max_chars: int = 96) -> str:
    """*value* as text, shortened in the middle to *max_chars*."""
    text = str(value)
    if len(text) <= max_chars:
        return text
    head = max_chars // 2 - 2
    tail = max_chars - head - 3
    return text[:head] + "..." + text[-tail:] if tail > 0 else text[:head] + "..."


def format_value(field: str, value: Any) -> str:
    """Log rendering of one field value."""
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if field in ("usb_vid", "usb_pid"):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return compact(value)
        return "0" if number == 0 else f"0x{number:04X}"
    if field in ("db_version", "shadow_db_version"):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return compact(value)
        return "0" if number == 0 else f"0x{number:X}"
    if isinstance(value, Mapping):
        if field in _FORMAT_FIELDS:
            ids = sorted(value, key=lambda item: str(item))
            shown = ",".join(str(item) for item in ids[:12])
            return f"{len(ids)}[{shown}{'...' if len(ids) > 12 else ''}]"
        keys = list(value)
        shown = ",".join(str(key) for key in keys[:8])
        return f"{len(keys)} keys[{shown}{'...' if len(keys) > 8 else ''}]"
    if isinstance(value, Iterable) and not isinstance(value, str):
        items = list(value)
        shown = ",".join(str(item) for item in items[:12])
        return f"{len(items)}[{shown}{'...' if len(items) > 12 else ''}]"
    return compact(value)


def format_fields(data: Mapping, fields=IDENTITY_FIELDS, *, include_false: bool = False) -> str:
    """``label=value`` pairs for the present fields of *data*."""
    parts: list[str] = []
    for field, label in fields:
        if field not in data:
            continue
        value = data[field]
        if is_missing(value):
            continue
        if isinstance(value, bool):
            if not value and not include_false:
                continue
        elif isinstance(value, (int, float)) and value == 0:
            continue
        parts.append(f"{label}={format_value(field, value)}")
    return ", ".join(parts) or "none"


def format_sources(sources: Mapping, fields=SOURCE_FIELDS) -> str:
    """``label:source`` pairs for the fields that have a source."""
    parts = [f"{label}:{sources[field]}" for field, label in fields if sources.get(field)]
    return ", ".join(parts) or "none"


def format_conflicts(conflicts: Any) -> str:
    """Short rendering of resolver conflicts (at most six)."""
    if not conflicts:
        return "none"
    if not isinstance(conflicts, list):
        return compact(conflicts)
    parts: list[str] = []
    for conflict in conflicts[:6]:
        if not isinstance(conflict, dict):
            parts.append(compact(conflict, max_chars=96))
            continue
        text = str(conflict.get("field", "?"))
        if conflict.get("winner"):
            text += f" winner={conflict['winner']}"
        text += (
            f" rejected={conflict.get('rejected_source') or '?'}:"
            f"{compact(conflict.get('rejected_value', ''), max_chars=36)}"
        )
        if conflict.get("reason"):
            text += f" reason={compact(conflict['reason'], max_chars=64)}"
        parts.append(text)
    if len(conflicts) > 6:
        parts.append(f"+{len(conflicts) - 6} more")
    return "; ".join(parts)
