"""Smart-playlist models and their MHOD 50 (preferences) / 51 (rules) encodings.

Also home to the opaque MHOD 102/55 passthroughs, and the converters from the
reader's parsed dicts back into the models (``prefs_from_row``/``rules_from_row``).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

from podsync.itdb.spec.layouts.strings import MHOD_HEADER_SIZE, write_mhod_header
from podsync.itdb.spec.smart_fields import (
    SLST_DEFAULT_UNK004,
    SLST_HEADER_SIZE,
    SPL_DATE_IDENTIFIER,
    SPL_DATE_RELATIVE_ACTION_IDS,
    SPL_GROUP_HEADER_BYTES_SIZE,
    SPL_GROUP_MARKER,
    SPLFT_DATE,
    SPLFT_STRING,
    SPLPREF_BODY_SIZE,
    spl_get_field_type,
)

__all__ = [
    "NestedRules",
    "SmartPrefs",
    "SmartRule",
    "SmartRuleSet",
    "prefs_from_row",
    "rules_from_row",
    "write_mhod50",
    "write_mhod51",
    "write_mhod55",
    "write_mhod102",
]

U32_MAX = 0xFFFF_FFFF
_LEAST_BIT = 0x80000000
_TEXT_PAYLOAD_LIMIT = 4096
_NUMERIC_PAYLOAD = struct.Struct(">QqQQqQIIIII")
_RULE_HEAD = struct.Struct(">III")


@dataclass
class SmartPrefs:
    """Smart playlist options (live update, limits, sort, match all/any) — ``mhod`` 50."""

    live_update: bool = True
    check_rules: bool = True
    check_limits: bool = False
    limit_type: int = 0x03  # songs
    limit_sort: int = 0x02  # random; high bit = reverse
    limit_value: int = 25
    match_checked_only: bool = False


@dataclass
class SmartRule:
    """One smart playlist condition: field, action and value(s)."""

    field_id: int = 0x02
    action_id: int = 0x01000002  # "contains"
    string_value: str | None = None
    from_value: int = 0
    from_date: int = 0
    from_units: int = 0
    to_value: int = 0
    to_date: int = 0
    to_units: int = 0
    unk052: int = 0
    unk056: int = 0
    unk060: int = 0
    unk064: int = 0
    unk068: int = 0


@dataclass
class SmartRuleSet:
    """The rules of a smart playlist and how they combine — ``mhod`` 51."""

    conjunction: str = "AND"
    rules: list[SmartRule | NestedRules] = field(default_factory=list)
    unk004: int = SLST_DEFAULT_UNK004


@dataclass
class NestedRules:
    """A parenthesized sub-expression inside a rule set."""

    group: SmartRuleSet = field(default_factory=SmartRuleSet)
    field_id: int = 0
    action_id: int = 1
    group_marker: int = SPL_GROUP_MARKER
    header_bytes: bytes | None = None  # 40 opaque bytes kept for byte-exact rewrites


# ── integer shaping ─────────────────────────────────────────────────


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _clamp(value: Any, low: int, high: int) -> int:
    return min(max(_int(value), low), high)


def u32(value: Any) -> int:
    """*value* clamped into the unsigned 32-bit range."""
    return _clamp(value, 0, U32_MAX)


def u64(value: Any) -> int:
    """*value* clamped into the unsigned 64-bit range."""
    return _clamp(value, 0, (1 << 64) - 1)


def _i64(value: Any) -> int:
    return _clamp(value, -(1 << 63), (1 << 63) - 1)


def _two_complement64(value: int) -> int:
    value &= (1 << 64) - 1
    return value - (1 << 64) if value >> 63 else value


# ── relative dates ──────────────────────────────────────────────────


def _is_relative_date(field_id: int, action_id: int) -> bool:
    return spl_get_field_type(field_id) == SPLFT_DATE and action_id in SPL_DATE_RELATIVE_ACTION_IDS


def _relative_from_parts(from_value: Any, from_date: Any, from_units: Any) -> tuple[int, int]:
    """Bring any historical spelling of "in the last N units" to ``(IDENTIFIER, -N)``.

    Newer files keep the count in ``from_date``; older ones stored a signed count
    (sometimes already multiplied by the unit) in ``from_value``.
    """
    count = _i64(from_date)
    if count:
        return SPL_DATE_IDENTIFIER, -abs(count)
    raw = _int(from_value)
    if raw == SPL_DATE_IDENTIFIER:
        return SPL_DATE_IDENTIFIER, 0
    amount = abs(_two_complement64(raw))
    if not amount:
        return SPL_DATE_IDENTIFIER, 0
    units = _int(from_units)
    if units > 1 and amount >= units and amount % units == 0:
        amount //= units
    return SPL_DATE_IDENTIFIER, -amount


# ── MHOD 50 ─────────────────────────────────────────────────────────


def write_mhod50(prefs: SmartPrefs) -> bytes:
    """Serialise smart playlist preferences."""
    limit_sort = u32(prefs.limit_sort)
    head = struct.pack(
        "<5B3xI2B",
        int(bool(prefs.live_update)), int(bool(prefs.check_rules)), int(bool(prefs.check_limits)),
        u32(prefs.limit_type) & 0xFF, limit_sort & 0xFF, u32(prefs.limit_value),
        int(bool(prefs.match_checked_only)), int(bool(limit_sort & _LEAST_BIT)),
    )
    body = head.ljust(SPLPREF_BODY_SIZE, b"\x00")
    return write_mhod_header(50, MHOD_HEADER_SIZE + len(body)) + body


# ── MHOD 51 ─────────────────────────────────────────────────────────


def _encode_rule(rule: SmartRule) -> bytes:
    field_id, action_id = u32(rule.field_id), u32(rule.action_id)
    if spl_get_field_type(field_id) == SPLFT_STRING and rule.string_value is not None:
        payload = str(rule.string_value).encode("utf-16-be", errors="replace")[:_TEXT_PAYLOAD_LIMIT]
    else:
        from_value, from_date = u64(rule.from_value), _i64(rule.from_date)
        to_value, to_date, to_units = u64(rule.to_value), _i64(rule.to_date), u64(rule.to_units)
        if _is_relative_date(field_id, action_id):
            from_value, from_date = _relative_from_parts(rule.from_value, rule.from_date, rule.from_units)
            to_value, to_date, to_units = SPL_DATE_IDENTIFIER, 0, 1
        payload = _NUMERIC_PAYLOAD.pack(
            from_value, from_date, u64(rule.from_units), to_value, to_date, to_units,
            *(u32(getattr(rule, f"unk0{n}")) for n in (52, 56, 60, 64, 68)),
        )
    return _RULE_HEAD.pack(field_id, action_id, 0) + bytes(SPL_GROUP_HEADER_BYTES_SIZE) + struct.pack(">I", len(payload)) + payload


def _encode_group(group: NestedRules) -> bytes:
    stamp = group.header_bytes if group.header_bytes is not None else bytes(SPL_GROUP_HEADER_BYTES_SIZE)
    if len(stamp) != SPL_GROUP_HEADER_BYTES_SIZE:
        raise ValueError("RuleGroup.header_bytes must contain exactly 40 bytes")
    nested = _encode_rule_set(group.group)
    head = _RULE_HEAD.pack(u32(group.field_id), u32(group.action_id), u32(group.group_marker))
    return head + bytes(stamp) + struct.pack(">I", len(nested)) + nested


def _encode_rule_set(rules: SmartRuleSet) -> bytes:
    encoded = [_encode_group(r) if isinstance(r, NestedRules) else _encode_rule(r) for r in rules.rules]
    conjunction = 1 if str(rules.conjunction).upper() == "OR" else 0
    header = (b"SLst" + struct.pack(">III", u32(rules.unk004), len(encoded), conjunction)).ljust(SLST_HEADER_SIZE, b"\x00")
    return header + b"".join(encoded)


def write_mhod51(rules_data: SmartRuleSet) -> bytes:
    """Serialise smart playlist rules (big-endian ``SLst`` body)."""
    body = _encode_rule_set(rules_data)
    return write_mhod_header(51, MHOD_HEADER_SIZE + len(body)) + body


def _passthrough(kind: int, raw_body: bytes) -> bytes:
    body = bytes(raw_body)
    return write_mhod_header(kind, MHOD_HEADER_SIZE + len(body)) + body


def write_mhod102(raw_body: bytes) -> bytes:
    """Re-emit a preserved ``mhod`` 102 body unchanged."""
    return _passthrough(102, raw_body)


def write_mhod55(raw_body: bytes) -> bytes:
    """Re-emit a preserved ``mhod`` 55 (playlist properties) body unchanged."""
    return _passthrough(55, raw_body)


# ── parsed rows → models ────────────────────────────────────────────


def prefs_from_row(parsed: dict) -> SmartPrefs:
    """Preferences from a parsed MHOD 50 dict; the reverse flag folds into ``limit_sort``."""
    parsed = parsed or {}
    base = SmartPrefs()
    limit_sort = u32(parsed.get("limit_sort", base.limit_sort))
    if parsed.get("reverse_sort"):
        limit_sort |= _LEAST_BIT
    flag = lambda name: bool(parsed.get(name, getattr(base, name)))  # noqa: E731
    number = lambda name: u32(parsed.get(name, getattr(base, name)))  # noqa: E731
    return SmartPrefs(
        live_update=flag("live_update"),
        check_rules=flag("check_rules"),
        check_limits=flag("check_limits"),
        limit_type=number("limit_type"),
        limit_sort=limit_sort,
        limit_value=number("limit_value"),
        match_checked_only=flag("match_checked_only"),
    )


def _conjunction_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return "OR" if value == 1 else "AND"
    return "AND"


def _group_from_row(rule: dict) -> NestedRules:
    stamp = rule.get("header_bytes")
    return NestedRules(
        group=rules_from_row(rule["group"]),
        field_id=u32(rule.get("field_id", 0)),
        action_id=u32(rule.get("action_id", 1)),
        group_marker=u32(rule.get("group_marker", SPL_GROUP_MARKER)),
        header_bytes=bytes(stamp) if isinstance(stamp, (bytes, bytearray)) else None,
    )


def _rule_from_row(rule: dict) -> SmartRule | NestedRules:
    if isinstance(rule.get("group"), dict):
        return _group_from_row(rule)
    field_id, action_id = u32(rule.get("field_id", 0)), u32(rule.get("action_id", 0))
    from_value, from_date = rule.get("from_value", 0), rule.get("from_date", 0)
    if _is_relative_date(field_id, action_id):
        from_value, from_date = _relative_from_parts(from_value, from_date, rule.get("from_units", 0))
    text = rule.get("string_value")
    return SmartRule(
        field_id=field_id,
        action_id=action_id,
        string_value=text if isinstance(text, str) else None,
        from_value=u64(from_value),
        from_date=_i64(from_date),
        from_units=u64(rule.get("from_units", 0)),
        to_value=u64(rule.get("to_value", 0)),
        to_date=_i64(rule.get("to_date", 0)),
        to_units=u64(rule.get("to_units", 0)),
        **{f"unk0{n}": u32(rule.get(f"unk0{n}", 0)) for n in (52, 56, 60, 64, 68)},
    )


def rules_from_row(parsed: dict) -> SmartRuleSet:
    """Rules from a parsed MHOD 51 dict (nested groups included)."""
    parsed = parsed or {}
    return SmartRuleSet(
        conjunction=_conjunction_text(parsed.get("conjunction", "AND")),
        rules=[_rule_from_row(rule) for rule in parsed.get("rules", []) or [] if isinstance(rule, dict)],
        unk004=u32(parsed.get("unk004", SLST_DEFAULT_UNK004)),
    )
