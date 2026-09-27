"""Smart-playlist vocabulary: MHOD 50 (preferences) and MHOD 51 (``SLst`` rules).

Rule fields, operators, limit kinds and sort orders as iTunes numbers them, plus
the host-side view of which track key each field reads when the rules are
evaluated on the computer (see :mod:`podsync.library.smart`).
"""

from __future__ import annotations

# ── body sizes ──────────────────────────────────────────────────────

SPLPREF_BODY_SIZE = 72  # MHOD 50 body
SLST_HEADER_SIZE = 136  # MHOD 51 body header (big-endian)
SLST_DEFAULT_UNK004 = 0x00010001
SPL_RULE_HEADER_SIZE = 56
SPL_RULE_DATA_SIZE = 0x44
SPL_GROUP_MARKER = 0x01000000  # rule whose data is a nested SLst
SPL_GROUP_HEADER_BYTES_OFFSET = 0x0C
SPL_GROUP_HEADER_BYTES_SIZE = 40

# ── limits ──────────────────────────────────────────────────────────

SPL_LIMIT_TYPE_MINUTES = 0x01
SPL_LIMIT_TYPE_MB = 0x02
SPL_LIMIT_TYPE_SONGS = 0x03
SPL_LIMIT_TYPE_HOURS = 0x04
SPL_LIMIT_TYPE_GB = 0x05
SPL_LIMIT_TYPE_MAP: dict[int, str] = {
    SPL_LIMIT_TYPE_MINUTES: "minutes",
    SPL_LIMIT_TYPE_MB: "MB",
    SPL_LIMIT_TYPE_SONGS: "songs",
    SPL_LIMIT_TYPE_HOURS: "hours",
    SPL_LIMIT_TYPE_GB: "GB",
}

# The high bit turns "most/highest" into "least/lowest".
_LEAST = 0x80000000
SPL_LIMIT_SORT_RANDOM = 0x02
SPL_LIMIT_SORT_SONG_NAME = 0x03
SPL_LIMIT_SORT_ALBUM = 0x04
SPL_LIMIT_SORT_ARTIST = 0x05
SPL_LIMIT_SORT_GENRE = 0x07
SPL_LIMIT_SORT_MOST_RECENTLY_ADDED = 0x10
SPL_LIMIT_SORT_LEAST_RECENTLY_ADDED = _LEAST | SPL_LIMIT_SORT_MOST_RECENTLY_ADDED
SPL_LIMIT_SORT_MOST_OFTEN_PLAYED = 0x14
SPL_LIMIT_SORT_LEAST_OFTEN_PLAYED = _LEAST | SPL_LIMIT_SORT_MOST_OFTEN_PLAYED
SPL_LIMIT_SORT_MOST_RECENTLY_PLAYED = 0x15
SPL_LIMIT_SORT_LEAST_RECENTLY_PLAYED = _LEAST | SPL_LIMIT_SORT_MOST_RECENTLY_PLAYED
SPL_LIMIT_SORT_HIGHEST_RATING = 0x17
SPL_LIMIT_SORT_LOWEST_RATING = _LEAST | SPL_LIMIT_SORT_HIGHEST_RATING
SPL_LIMIT_SORT_MAP: dict[int, str] = {
    SPL_LIMIT_SORT_RANDOM: "random",
    SPL_LIMIT_SORT_SONG_NAME: "song_name",
    SPL_LIMIT_SORT_ALBUM: "album",
    SPL_LIMIT_SORT_ARTIST: "artist",
    SPL_LIMIT_SORT_GENRE: "genre",
    SPL_LIMIT_SORT_MOST_RECENTLY_ADDED: "most_recently_added",
    SPL_LIMIT_SORT_LEAST_RECENTLY_ADDED: "least_recently_added",
    SPL_LIMIT_SORT_MOST_OFTEN_PLAYED: "most_often_played",
    SPL_LIMIT_SORT_LEAST_OFTEN_PLAYED: "least_often_played",
    SPL_LIMIT_SORT_MOST_RECENTLY_PLAYED: "most_recently_played",
    SPL_LIMIT_SORT_LEAST_RECENTLY_PLAYED: "least_recently_played",
    SPL_LIMIT_SORT_HIGHEST_RATING: "highest_rating",
    SPL_LIMIT_SORT_LOWEST_RATING: "lowest_rating",
}

# ── dates ───────────────────────────────────────────────────────────

SPL_DATE_RELATIVE_ACTION_IDS = frozenset({0x00000200, 0x02000200})  # "is (not) in the last"
SPL_DATE_IDENTIFIER = 0x2DAE2DAE2DAE2DAE  # from/to value marking a relative date
SPL_DATE_UNITS_MAP: dict[int, str] = {
    1: "seconds", 60: "minutes", 3_600: "hours", 86_400: "days",
    604_800: "weeks", 2_628_000: "months",
}

# ── rule fields ─────────────────────────────────────────────────────

SPLFT_STRING = 1
SPLFT_INT = 2
SPLFT_BOOLEAN = 3
SPLFT_DATE = 4
SPLFT_PLAYLIST = 5
SPLFT_UNKNOWN = 6
SPLFT_BINARY_AND = 7

_S, _I, _B, _D, _P, _M = SPLFT_STRING, SPLFT_INT, SPLFT_BOOLEAN, SPLFT_DATE, SPLFT_PLAYLIST, SPLFT_BINARY_AND

# field id: (label shown by iTunes, value type)
_RULE_FIELDS: dict[int, tuple[str, int]] = {
    0x02: ("Song Name", _S),
    0x03: ("Album", _S),
    0x04: ("Artist", _S),
    0x05: ("Bit Rate", _I),
    0x06: ("Sample Rate", _I),
    0x07: ("Year", _I),
    0x08: ("Genre", _S),
    0x09: ("Kind", _S),
    0x0A: ("Date Modified", _D),
    0x0B: ("Track Number", _I),
    0x0C: ("Size", _I),
    0x0D: ("Time", _I),
    0x0E: ("Comment", _S),
    0x10: ("Date Added", _D),
    0x12: ("Composer", _S),
    0x16: ("Plays", _I),
    0x17: ("Last Played", _D),
    0x18: ("Disc Number", _I),
    0x19: ("Rating", _I),
    0x1D: ("Checked", _B),
    0x1F: ("Compilation", _B),
    0x23: ("BPM", _I),
    0x25: ("Album Artwork", _B),
    0x27: ("Grouping", _S),
    0x28: ("Playlist", _P),
    0x29: ("Purchased", _B),
    0x36: ("Description", _S),
    0x37: ("Category", _S),
    0x39: ("Podcast", _I),
    0x3C: ("Media Kind", _I),
    0x3E: ("TV Show", _S),
    0x3F: ("Season Number", _I),
    0x44: ("Skips", _I),
    0x45: ("Last Skipped", _D),
    0x47: ("Album Artist", _S),
    0x4E: ("Sort Song Name", _S),
    0x4F: ("Sort Album", _S),
    0x50: ("Sort Artist", _S),
    0x51: ("Sort Album Artist", _S),
    0x52: ("Sort Composer", _S),
    0x53: ("Sort TV Show", _S),
    0x59: ("Video Rating", _S),
    0x5A: ("Album Rating", _I),
    0x85: ("Location", _M),
    0x86: ("Cloud Status", _I),
    0x9A: ("Favorite / Suggest Less", _I),
    0x9C: ("Album Favorite / Suggest Less", _I),
    0x9F: ("Work", _S),
    0xA0: ("Movement Name", _S),
    0xA1: ("Movement Number", _I),
}
SPL_FIELD_MAP: dict[int, str] = {field: label for field, (label, _kind) in _RULE_FIELDS.items()}
SPL_FIELD_TYPE_MAP: dict[int, int] = {field: kind for field, (_label, kind) in _RULE_FIELDS.items()}


def spl_get_field_type(field_id: int) -> int:
    return SPL_FIELD_TYPE_MAP.get(field_id, SPLFT_UNKNOWN)


# ── operators ───────────────────────────────────────────────────────
# Bits 24–25 of an action select the family: 0 number/date, 1 text, 2 negated
# number/date, 3 negated text.  The low bits select the comparison.

_NUMBER_OPS = {
    0x001: ("is", "is not"),
    0x010: ("is greater than", "is not greater than"),
    0x020: ("is greater than or equal to", "is not greater than or equal to"),
    0x040: ("is less than", "is not less than"),
    0x080: ("is less than or equal to", "is not less than or equal to"),
    0x100: ("is in the range", "is not in the range"),
    0x200: ("is in the last", "is not in the last"),
    0x400: ("binary AND", "not binary AND"),
}
_TEXT_OPS = {
    0x1: ("is (string)", "is not (string)"),
    0x2: ("contains", "does not contain"),
    0x4: ("begins with", "does not begin with"),
    0x8: ("ends with", "does not end with"),
}
SPL_ACTION_MAP: dict[int, str] = {}
for _low, (_plain, _negated) in _NUMBER_OPS.items():
    SPL_ACTION_MAP[0x00000000 | _low] = _plain
    SPL_ACTION_MAP[0x02000000 | _low] = _negated
for _low, (_plain, _negated) in _TEXT_OPS.items():
    SPL_ACTION_MAP[0x01000000 | _low] = _plain
    SPL_ACTION_MAP[0x03000000 | _low] = _negated
SPL_ACTION_MAP[0x00000800] = "binary unknown1"
SPL_ACTION_MAP[0x02000800] = "binary unknown2"
del _low, _plain, _negated

# ── host-side evaluation ────────────────────────────────────────────
# Which rule-view key (podsync.library.tracks.rule_view_of) each field reads.

SPL_HOST_STRING_FIELD_KEYS: dict[int, str] = {
    0x02: "title", 0x03: "album", 0x04: "artist", 0x08: "genre", 0x09: "filetype",
    0x0E: "comment", 0x12: "composer", 0x27: "grouping", 0x36: "description",
    0x37: "category", 0x3E: "show", 0x47: "album_artist", 0x4E: "sort_title",
    0x4F: "sort_album", 0x50: "sort_artist", 0x51: "sort_album_artist",
    0x52: "sort_composer", 0x53: "sort_show",
}
SPL_HOST_INT_FIELD_KEYS: dict[int, str] = {
    0x05: "bitrate", 0x06: "sample_rate", 0x07: "year", 0x0B: "track_number",
    0x0C: "size", 0x0D: "length", 0x16: "play_count", 0x18: "disc_number",
    0x19: "rating", 0x23: "bpm", 0x39: "podcast_flag", 0x3C: "media_type",
    0x3F: "season_number", 0x44: "skip_count",
}
SPL_HOST_DATE_FIELD_KEYS: dict[int, str] = {
    0x0A: "last_modified", 0x10: "date_added", 0x17: "last_played", 0x45: "last_skipped",
}
SPL_HOST_BOOLEAN_FIELD_KEYS: dict[int, str] = {
    0x1D: "checked_flag", 0x1F: "compilation_flag", 0x25: "has_artwork", 0x29: "purchased_flag",
}
SPL_HOST_BINARY_AND_FIELD_KEYS: dict[int, str] = {0x85: "location_kind"}

SPL_HOST_EVALUABLE_FIELD_IDS = frozenset().union(
    SPL_HOST_STRING_FIELD_KEYS, SPL_HOST_INT_FIELD_KEYS, SPL_HOST_DATE_FIELD_KEYS,
    SPL_HOST_BOOLEAN_FIELD_KEYS, SPL_HOST_BINARY_AND_FIELD_KEYS, {0x28},
)
# Podcast, TV Show and Season Number evaluate but cannot be authored here.
SPL_AUTHORABLE_FIELD_IDS = SPL_HOST_EVALUABLE_FIELD_IDS - {0x39, 0x3E, 0x3F}

# Fields whose value is picked from a fixed list rather than typed.
SPL_CHOICE_FIELD_IDS = frozenset({0x28, 0x3C, 0x85, 0x86, 0x9A, 0x9C})
_FAVORITE_CHOICES = ((2, "Favorite"), (3, "Suggest Less"), (0, "None"))
SPL_CHOICE_VALUE_MAP: dict[int, tuple[tuple[int, str], ...]] = {
    0x9A: _FAVORITE_CHOICES,
    0x9C: _FAVORITE_CHOICES,
    0x86: (
        (2, "Matched"), (1, "Purchased"), (3, "Uploaded"), (4, "Ineligible"),
        (5, "Removed"), (6, "Error"), (7, "Duplicate"), (8, "Apple Music"),
        (9, "No Longer Available"), (10, "Not Uploaded"),
    ),
    0x85: ((1, "on this computer"), (2, "iCloud")),
    0x3C: (
        (0x01, "Music"), (0x20, "Music Video"), (0x02, "Movie"), (0x40, "TV Show"),
        (0x04, "Podcast"), (0x08, "Audiobook"), (0x100000, "Voice Memo"),
        (0x10000, "iTunes Extras"),
    ),
}
SPL_CHOICE_UNKNOWN_LABELS: dict[int, tuple[str, ...]] = {0x3C: ("Home Video",)}
