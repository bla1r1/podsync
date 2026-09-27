"""Smart-playlist engine: decide which tracks a rule set selects.

Tracks are plain dicts — either parsed rows or :func:`podsync.library.tracks.rule_view_of`
projections.  Rules come from :mod:`podsync.itdb.writer.smart_rules`.  The engine is
pure apart from ``time.time()`` (relative dates) and ``random`` (random limit order).
"""

from __future__ import annotations

import logging
import operator
import random
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from podsync.itdb.spec.clock import DeviceClock, active_device_clock
from podsync.itdb.spec.fields import MAC_EPOCH_OFFSET
from podsync.itdb.spec.smart_fields import (
    SPL_DATE_RELATIVE_ACTION_IDS,
    SPL_FIELD_TYPE_MAP,
    SPL_HOST_BINARY_AND_FIELD_KEYS,
    SPL_HOST_BOOLEAN_FIELD_KEYS,
    SPL_HOST_DATE_FIELD_KEYS,
    SPL_HOST_INT_FIELD_KEYS,
    SPL_HOST_STRING_FIELD_KEYS,
    SPL_LIMIT_SORT_ALBUM,
    SPL_LIMIT_SORT_ARTIST,
    SPL_LIMIT_SORT_GENRE,
    SPL_LIMIT_SORT_HIGHEST_RATING,
    SPL_LIMIT_SORT_MOST_OFTEN_PLAYED,
    SPL_LIMIT_SORT_MOST_RECENTLY_ADDED,
    SPL_LIMIT_SORT_MOST_RECENTLY_PLAYED,
    SPL_LIMIT_SORT_RANDOM,
    SPL_LIMIT_SORT_SONG_NAME,
    SPL_LIMIT_TYPE_GB,
    SPL_LIMIT_TYPE_HOURS,
    SPL_LIMIT_TYPE_MB,
    SPL_LIMIT_TYPE_MINUTES,
    SPL_LIMIT_TYPE_SONGS,
    SPLFT_BINARY_AND,
    SPLFT_BOOLEAN,
    SPLFT_DATE,
    SPLFT_INT,
    SPLFT_PLAYLIST,
    SPLFT_STRING,
)
from podsync.itdb.writer.smart_rules import (
    NestedRules,
    SmartPrefs,
    SmartRule,
    SmartRuleSet,
    prefs_from_row,
    rules_from_row,
)

__all__ = [
    "evaluate_all_smart_playlists",
    "evaluate_parsed_smart_playlist",
    "evaluate_smart_playlist",
    "rule_matches",
]

logger = logging.getLogger(__name__)

Track = Mapping[str, Any]
Membership = Mapping[int, set[int]]

# Media-kind rules use the binary "includes/excludes" operators on an INT field.
_MEDIA_KIND_FIELD = 0x3C
_BINARY_ACTIONS = (0x00000400, 0x02000400)
_RELATIVE_AFTER = 0x00000200  # "is in the last"; its negation is "is not in the last"

# ── reading one field out of a track ────────────────────────────────


def _text_of(track: Track, field_id: int) -> str:
    key = SPL_HOST_STRING_FIELD_KEYS.get(field_id)
    value = track.get(key, "") if key else ""
    return value.casefold() if isinstance(value, str) else ""


def _int_from(track: Track, key: str | None) -> int:
    value = track.get(key, 0) if key else 0
    return int(value) if isinstance(value, int) else 0


def _number_of(track: Track, field_id: int) -> int:
    key = SPL_HOST_INT_FIELD_KEYS.get(field_id) or SPL_HOST_BINARY_AND_FIELD_KEYS.get(field_id)
    return _int_from(track, key)


def _timestamp_of(track: Track, field_id: int) -> int:
    return _int_from(track, SPL_HOST_DATE_FIELD_KEYS.get(field_id))


# Boolean fields whose truth is derived rather than read from a single key.
_DERIVED_TRUTH: dict[int, Callable[[Track], bool]] = {
    0x1D: lambda t: t.get("checked_flag", 0) == 0,  # stored 0 means "checked"
    0x25: lambda t: bool(t.get("has_artwork") or t.get("artwork_count") or t.get("artwork_link")),
    0x29: lambda t: bool(t.get("purchased_flag") or t.get("purchased")),
}


def _truth_of(track: Track, field_id: int) -> bool:
    derived = _DERIVED_TRUTH.get(field_id)
    if derived is not None:
        return derived(track)
    key = SPL_HOST_BOOLEAN_FIELD_KEYS.get(field_id)
    return bool(track.get(key, 0)) if key else False


# ── operators ───────────────────────────────────────────────────────

_TEXT_TESTS: dict[int, Callable[[str, str], bool]] = {
    0x01000001: operator.eq,
    0x03000001: operator.ne,
    0x01000002: lambda have, want: want in have,
    0x03000002: lambda have, want: want not in have,
    0x01000004: str.startswith,
    0x03000004: lambda have, want: not have.startswith(want),
    0x01000008: str.endswith,
    0x03000008: lambda have, want: not have.endswith(want),
}


def _inside(value: int, a: int, b: int) -> bool:
    return min(a, b) <= value <= max(a, b)


# Comparisons shared by integers and absolute dates: f(value, from, to).
_ORDER_TESTS: dict[int, Callable[[int, int, int], bool]] = {
    0x00000010: lambda v, a, _b: v > a,
    0x02000010: lambda v, a, _b: v <= a,
    0x00000040: lambda v, a, _b: v < a,
    0x02000040: lambda v, a, _b: v >= a,
    0x00000100: _inside,
    0x02000100: lambda v, a, b: not _inside(v, a, b),
}
_INT_TESTS = {
    **_ORDER_TESTS,
    0x00000001: lambda v, a, _b: v == a,
    0x02000001: lambda v, a, _b: v != a,
}
# An absolute "is" covers the span [from, to]; a zero upper bound means the single point.
_DATE_TESTS = {
    **_ORDER_TESTS,
    0x00000001: lambda v, a, b: a <= v <= (b or a),
    0x02000001: lambda v, a, b: not (a <= v <= (b or a)),
}
_BOOL_TESTS: dict[int, Callable[[bool], bool]] = {0x00000001: bool, 0x02000001: operator.not_}
_MASK_TESTS: dict[int, Callable[[int, int], bool]] = {
    0x00000400: lambda v, mask: bool(v & mask),
    0x02000400: lambda v, mask: not v & mask,
}
_MEMBER_TESTS: dict[int, Callable[[int, set[int]], bool]] = {
    0x00000001: lambda tid, members: tid in members,
    0x02000001: lambda tid, members: tid not in members,
}


def _test_text(rule: SmartRule, track: Track) -> bool:
    test = _TEXT_TESTS.get(rule.action_id)
    if test is None or rule.string_value is None:
        return False
    return test(_text_of(track, rule.field_id), str(rule.string_value).casefold())


def _test_int(rule: SmartRule, track: Track) -> bool:
    test = _INT_TESTS.get(rule.action_id)
    return bool(test and test(_number_of(track, rule.field_id), rule.from_value, rule.to_value))


def _test_mask(rule: SmartRule, track: Track) -> bool:
    test = _MASK_TESTS.get(rule.action_id)
    return bool(test and test(_number_of(track, rule.field_id), rule.from_value))


def _test_bool(rule: SmartRule, track: Track) -> bool:
    test = _BOOL_TESTS.get(rule.action_id)
    return bool(test and test(_truth_of(track, rule.field_id)))


def _as_unix(value: Any, clock: DeviceClock | None) -> int:
    """Rule dates at or past the Mac epoch offset are device-local Mac times."""
    value = int(value or 0)
    if value < MAC_EPOCH_OFFSET:
        return value
    return (clock or active_device_clock()).mac_to_unix(value)


def _test_date(rule: SmartRule, track: Track, clock: DeviceClock) -> bool:
    value = _timestamp_of(track, rule.field_id)
    if rule.action_id in SPL_DATE_RELATIVE_ACTION_IDS:
        # from/to hold format sentinels here; only the (count × unit) offset matters.
        cutoff = int(time.time()) + rule.from_date * rule.from_units
        recent = value > cutoff
        return recent if rule.action_id == _RELATIVE_AFTER else not recent
    test = _DATE_TESTS.get(rule.action_id)
    if test is None:
        return False
    return test(value, _as_unix(rule.from_value, clock), _as_unix(rule.to_value, clock))


def _test_membership(rule: SmartRule, track: Track, lookup: Membership | None) -> bool:
    test = _MEMBER_TESTS.get(rule.action_id)
    if test is None or lookup is None:
        return False
    return test(track.get("track_id", 0), lookup.get(rule.from_value, set()))


# ── dispatch ────────────────────────────────────────────────────────


def _group_matches(
    rules: SmartRuleSet, track: Track, lookup: Membership | None, clock: DeviceClock | None,
) -> bool:
    outcomes = (rule_matches(child, track, lookup, clock) for child in rules.rules)
    return any(outcomes) if str(rules.conjunction).upper() == "OR" else all(outcomes)


def rule_matches(
    rule: SmartRule | NestedRules,
    track: dict,
    playlist_lookup: dict[int, set[int]] | None = None,
    time_context: DeviceClock | None = None,
) -> bool:
    """Evaluate one rule (or nested group) against one track."""
    if isinstance(rule, NestedRules):
        return _group_matches(rule.group, track, playlist_lookup, time_context)
    if rule.field_id == _MEDIA_KIND_FIELD and rule.action_id in _BINARY_ACTIONS:
        return _test_mask(rule, track)
    kind = SPL_FIELD_TYPE_MAP.get(rule.field_id)
    if kind == SPLFT_STRING:
        return _test_text(rule, track)
    if kind == SPLFT_INT:
        return _test_int(rule, track)
    if kind == SPLFT_BOOLEAN:
        return _test_bool(rule, track)
    if kind == SPLFT_DATE:
        return _test_date(rule, track, time_context or active_device_clock())
    if kind == SPLFT_PLAYLIST:
        return _test_membership(rule, track, playlist_lookup)
    if kind == SPLFT_BINARY_AND:
        return _test_mask(rule, track)
    return False


# ── limits ──────────────────────────────────────────────────────────

_MIB, _GIB = 1024**2, 1024**3
_LIMIT_MEASURES: dict[int, Callable[[Track], float]] = {
    SPL_LIMIT_TYPE_MINUTES: lambda t: (t.get("length", 0) or 0) / 60_000,
    SPL_LIMIT_TYPE_MB: lambda t: (t.get("size", 0) or 0) / _MIB,
    SPL_LIMIT_TYPE_SONGS: lambda _t: 1.0,
    SPL_LIMIT_TYPE_HOURS: lambda t: (t.get("length", 0) or 0) / 3_600_000,
    SPL_LIMIT_TYPE_GB: lambda t: (t.get("size", 0) or 0) / _GIB,
}
_TEXT_ORDERS = {
    SPL_LIMIT_SORT_SONG_NAME: "title",
    SPL_LIMIT_SORT_ALBUM: "album",
    SPL_LIMIT_SORT_ARTIST: "artist",
    SPL_LIMIT_SORT_GENRE: "genre",
}
_NUMBER_ORDERS = {
    SPL_LIMIT_SORT_MOST_RECENTLY_ADDED: "date_added",
    SPL_LIMIT_SORT_MOST_OFTEN_PLAYED: "play_count",
    SPL_LIMIT_SORT_MOST_RECENTLY_PLAYED: "last_played",
    SPL_LIMIT_SORT_HIGHEST_RATING: "rating",
}
_LEAST_BIT = 0x80000000


def _order_selection(tracks: list[Track], limit_sort: int) -> list[Track]:
    """Order candidates before the limit is applied.

    The high bit flips "most/highest" to "least/lowest" for numeric orders; text
    orders are always ascending.  Unknown orders keep the input order.
    """
    base = limit_sort & ~_LEAST_BIT
    if base == SPL_LIMIT_SORT_RANDOM:
        shuffled = list(tracks)
        random.shuffle(shuffled)
        return shuffled
    if base in _TEXT_ORDERS:
        key = _TEXT_ORDERS[base]
        return sorted(tracks, key=lambda t: (t.get(key, "") or "").casefold())
    if base in _NUMBER_ORDERS:
        key = _NUMBER_ORDERS[base]
        return sorted(tracks, key=lambda t: t.get(key, 0), reverse=not limit_sort & _LEAST_BIT)
    return list(tracks)


def _apply_limit(tracks: Iterable[Track], prefs: SmartPrefs) -> list[Track]:
    """Greedy fill: take each track that still fits; skip (but keep scanning) the rest."""
    measure = _LIMIT_MEASURES.get(prefs.limit_type, _LIMIT_MEASURES[SPL_LIMIT_TYPE_SONGS])
    taken: list[Track] = []
    used = 0.0
    for track in tracks:
        cost = measure(track)
        if used + cost <= prefs.limit_value:
            taken.append(track)
            used += cost
    return taken


# ── entry points ────────────────────────────────────────────────────


def evaluate_smart_playlist(
    prefs: SmartPrefs,
    rules: SmartRuleSet,
    tracks: list[dict],
    playlist_lookup: dict[int, set[int]] | None = None,
    time_context: DeviceClock | None = None,
) -> list[int]:
    """Return the ``track_id`` values a smart playlist selects, in selection order.

    Disabled or empty rules select everything eligible; "checked only" drops
    unchecked tracks first.  Limits sort, then greedily fill up to the limit.
    """
    use_rules = bool(prefs.check_rules and rules.rules)
    chosen = [
        track for track in tracks
        if not (prefs.match_checked_only and track.get("checked_flag", 0) != 0)
        and (not use_rules or _group_matches(rules, track, playlist_lookup, time_context))
    ]
    if chosen and prefs.check_limits:
        chosen = _apply_limit(_order_selection(chosen, prefs.limit_sort), prefs)
    return [track["track_id"] for track in chosen if "track_id" in track]


def evaluate_parsed_smart_playlist(
    parsed_prefs: dict,
    parsed_rules: dict,
    tracks: list[dict],
    playlist_lookup=None,
    time_context=None,
) -> list[int]:
    """:func:`evaluate_smart_playlist` for raw parsed MHOD 50/51 payloads."""
    return evaluate_smart_playlist(
        prefs_from_row(parsed_prefs), rules_from_row(parsed_rules),
        tracks, playlist_lookup, time_context,
    )


def evaluate_all_smart_playlists(
    playlists: list[dict], tracks: list[dict], live_only: bool = False,
) -> dict[str, list[int]]:
    """Evaluate every smart row of a parsed playlist list, keyed by title.

    Membership rules see the parsed items of each playlist (parser track ids);
    playlists with no items get no lookup entry.
    """
    lookup = {
        row.get("playlist_id", 0): {item.get("track_id", 0) for item in row["items"]}
        for row in playlists if row.get("items")
    }
    results: dict[str, list[int]] = {}
    for row in playlists:
        prefs, rules = row.get("smart_playlist_data"), row.get("smart_playlist_rules")
        if not prefs or rules is None:
            continue
        if live_only and not prefs.get("live_update", False):
            continue
        results[row.get("title", "?")] = evaluate_parsed_smart_playlist(prefs, rules, tracks, lookup)
    logger.debug("Evaluated %d smart playlists over %d tracks", len(results), len(tracks))
    return results
