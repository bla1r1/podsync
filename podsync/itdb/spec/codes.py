"""Numeric vocabulary of the iTunesDB format: chunk tags, datasets, MHOD kinds, media kinds.

Everything here is data about the file format; nothing reads or writes files.
"""

from __future__ import annotations

# ── chunks and datasets ─────────────────────────────────────────────

CHUNK_TAGS = tuple(
    tag.encode("ascii")
    for tag in ("mhbd", "mhsd", "mhlt", "mhit", "mhlp", "mhla", "mhli", "mhyp", "mhod", "mhip", "mhia", "mhii")
)

# MHSD dataset type -> key under which split_datasets() returns its rows.
DATASET_RESULT_KEYS: dict[int, str] = {
    1: "mhlt",  # tracks
    2: "mhlp",  # visible playlists
    3: "mhlp_podcast",  # playlists with podcast grouping
    4: "mhla",  # albums
    5: "mhlp_smart",  # categories / smart playlists
    **{kind: f"mhsd_type_{kind}" for kind in range(6, 11)},
}

CHUNK_LABELS: dict[str, str] = {
    "mhbd": "Database",
    "mhsd": "Dataset",
    "mhlt": "Track List",
    "mhlp": "Playlist or Podcast List",
    "mhla": "Album List",
    "mhli": "Artist List",
    "mhlp_smart": "Smart Playlist List",
    "mhia": "Album Item",
    "mhii": "Artist Item",
    "mhit": "Track Item",
    "mhyp": "Playlist",
    "mhod": "Data Object",
    "mhip": "Playlist Item",
}

# ── database version numbers (MHBD +0x10) ───────────────────────────

_RELEASES = (
    "1.0", "2.0", "3.0", "4.0", "4.0.1", "4.1", "4.1.1", "4.1.2", "4.2", "4.5", "4.7",
    "4.71/4.8", "4.9", "5", "6", "6.0.1", "6.0.2-6.0.4", "6.0.5", "7.0", "7.1", "7.2",
    None, "7.3.0", "7.3.1-7.3.2", "7.4", "7.4.1", "7.4.2", "7.5", "7.6", "7.7", "8.0",
    "8.0.1", "8.0.2", "8.1", "8.1.1", "8.2", "8.2.1", "9.0", "9.0.1", "9.0.2", "9.0.3",
    "9.1", "9.1.1", "9.2", "9.2.1",
)
ITUNES_VERSIONS: dict[int, str] = {
    number: (f"iTunes {release}" if release else f"Unknown ({number:#04x})")
    for number, release in enumerate(_RELEASES, start=1)
}
ITUNES_VERSIONS.update({
    0x30: "iTunes 9.2+", 0x40: "iTunes 10.x", 0x50: "iTunes 11.x",
    0x60: "iTunes 12.x", 0x70: "iTunes 12.5+", 0x75: "iTunes 12.9+",
})


def itunes_version_label(version_hex: int | str) -> str:
    """Human-readable iTunes release for a database version (``"0x19"``, ``"25"`` or ``25``)."""
    if isinstance(version_hex, str):
        version_hex = int(version_hex, 16) if version_hex.startswith("0x") else int(version_hex)
    if version_hex in ITUNES_VERSIONS:
        return ITUNES_VERSIONS[version_hex]
    older = [known for known in ITUNES_VERSIONS if known <= version_hex]
    if older:
        return f"{ITUNES_VERSIONS[max(older)]} (or newer)"
    return f"Unknown (version {hex(version_hex)})"


# ── MHOD kinds ──────────────────────────────────────────────────────

MHOD_TYPE_TITLE = 1
MHOD_TYPE_LOCATION = 2
MHOD_TYPE_ALBUM = 3
MHOD_TYPE_ARTIST = 4
MHOD_TYPE_GENRE = 5
MHOD_TYPE_FILETYPE = 6
MHOD_TYPE_EQ_SETTING = 7
MHOD_TYPE_COMMENT = 8
MHOD_TYPE_CATEGORY = 9
MHOD_TYPE_LYRICS = 10
MHOD_TYPE_COMPOSER = 12
MHOD_TYPE_GROUPING = 13
MHOD_TYPE_DESCRIPTION = 14
MHOD_TYPE_PODCAST_ENCLOSURE_URL = 15
MHOD_TYPE_PODCAST_RSS_URL = 16
MHOD_TYPE_CHAPTER_DATA = 17
MHOD_TYPE_SUBTITLE = 18
MHOD_TYPE_SHOW_NAME = 19
MHOD_TYPE_EPISODE_ID = 20
MHOD_TYPE_NETWORK_NAME = 21
MHOD_TYPE_ALBUM_ARTIST = 22
MHOD_TYPE_SORT_ARTIST = 23
MHOD_TYPE_KEYWORDS = 24
MHOD_TYPE_SHOW_LOCALE = 25
MHOD_TYPE_SORT_NAME = 27
MHOD_TYPE_SORT_ALBUM = 28
MHOD_TYPE_SORT_ALBUM_ARTIST = 29
MHOD_TYPE_SORT_COMPOSER = 30
MHOD_TYPE_SORT_SHOW = 31
MHOD_TYPE_SMART_PLAYLIST_DATA = 50
MHOD_TYPE_SMART_PLAYLIST_RULES = 51
MHOD_TYPE_LIBRARY_PLAYLIST_INDEX = 52
MHOD_TYPE_LIBRARY_PLAYLIST_JUMP_TABLE = 53
MHOD_TYPE_PLAYLIST_PROPERTY_PLIST = 55
MHOD_TYPE_COLUMN_SIZE_OR_ORDER = 100
MHOD_TYPE_PLAYLIST_SETTINGS = 102
MHOD_TYPE_ALBUM_ALBUM = 200
MHOD_TYPE_ALBUM_ARTIST_ITEM = 201
MHOD_TYPE_ALBUM_SORT_ARTIST = 202
MHOD_TYPE_ALBUM_PODCAST_URL = 203
MHOD_TYPE_ALBUM_SHOW = 204
MHOD_TYPE_ARTIST_NAME = 300

# MHOD kind -> key of the flattened record it fills (strings) or labels (binary kinds).
MHOD_FIELD_KEYS: dict[int, str] = {
    MHOD_TYPE_TITLE: "title",
    MHOD_TYPE_LOCATION: "location",
    MHOD_TYPE_ALBUM: "album",
    MHOD_TYPE_ARTIST: "artist",
    MHOD_TYPE_GENRE: "genre",
    MHOD_TYPE_FILETYPE: "kind",
    MHOD_TYPE_EQ_SETTING: "eq_setting",
    MHOD_TYPE_COMMENT: "comment",
    MHOD_TYPE_CATEGORY: "category",
    MHOD_TYPE_LYRICS: "lyrics",
    MHOD_TYPE_COMPOSER: "composer",
    MHOD_TYPE_GROUPING: "grouping",
    MHOD_TYPE_DESCRIPTION: "description",
    MHOD_TYPE_PODCAST_ENCLOSURE_URL: "enclosure_url",
    MHOD_TYPE_PODCAST_RSS_URL: "feed_url",
    MHOD_TYPE_CHAPTER_DATA: "chapter_blob",
    MHOD_TYPE_SUBTITLE: "subtitle",
    MHOD_TYPE_SHOW_NAME: "show",
    MHOD_TYPE_EPISODE_ID: "episode",
    MHOD_TYPE_NETWORK_NAME: "network",
    MHOD_TYPE_ALBUM_ARTIST: "album_artist",
    MHOD_TYPE_SORT_ARTIST: "sort_artist",
    MHOD_TYPE_KEYWORDS: "keywords",
    MHOD_TYPE_SHOW_LOCALE: "show_locale",
    26: "store_asset_info",
    MHOD_TYPE_SORT_NAME: "sort_title",
    MHOD_TYPE_SORT_ALBUM: "sort_album",
    MHOD_TYPE_SORT_ALBUM_ARTIST: "sort_album_artist",
    MHOD_TYPE_SORT_COMPOSER: "sort_composer",
    MHOD_TYPE_SORT_SHOW: "sort_show",
    32: "video_unknown",
    37: "content_provider",
    39: "copyright",
    42: "encoding_quality",
    43: "purchase_account",
    44: "purchaser_name",
    MHOD_TYPE_SMART_PLAYLIST_DATA: "smart_prefs_record",
    MHOD_TYPE_SMART_PLAYLIST_RULES: "smart_rules_record",
    MHOD_TYPE_LIBRARY_PLAYLIST_INDEX: "library_index",
    MHOD_TYPE_LIBRARY_PLAYLIST_JUMP_TABLE: "library_jump_table",
    MHOD_TYPE_PLAYLIST_PROPERTY_PLIST: "playlist_property_plist",
    MHOD_TYPE_COLUMN_SIZE_OR_ORDER: "playlist_order",
    MHOD_TYPE_PLAYLIST_SETTINGS: "playlist_settings_blob",
    MHOD_TYPE_ALBUM_ALBUM: "album_name",
    MHOD_TYPE_ALBUM_ARTIST_ITEM: "album_artist_name",
    MHOD_TYPE_ALBUM_SORT_ARTIST: "album_sort_artist",
    MHOD_TYPE_ALBUM_PODCAST_URL: "album_feed_url",
    MHOD_TYPE_ALBUM_SHOW: "album_show",
    MHOD_TYPE_ARTIST_NAME: "artist_name",
}
# Kinds seen in the wild whose meaning is unknown keep a numbered key.
MHOD_FIELD_KEYS.update({kind: f"unknown_{kind}" for kind in (33, 34, 35, 36, 38, 40, 41)})

# ── media kinds (MHIT +0xD0, a bit mask) ────────────────────────────

MEDIA_TYPE_AUDIO_VIDEO = 0x00
MEDIA_TYPE_AUDIO = 1 << 0
MEDIA_TYPE_VIDEO = 1 << 1
MEDIA_TYPE_PODCAST = 1 << 2
MEDIA_TYPE_VIDEO_PODCAST = MEDIA_TYPE_VIDEO | MEDIA_TYPE_PODCAST
MEDIA_TYPE_AUDIOBOOK = 1 << 3
MEDIA_TYPE_MUSIC_VIDEO = 1 << 5
MEDIA_TYPE_TV_SHOW = 1 << 6
MEDIA_TYPE_TV_SHOW_ALT = MEDIA_TYPE_TV_SHOW | MEDIA_TYPE_MUSIC_VIDEO
MEDIA_TYPE_RINGTONE = 1 << 14
MEDIA_TYPE_RENTAL = 1 << 15
MEDIA_TYPE_ITUNES_EXTRA = 1 << 16
MEDIA_TYPE_MEMO = 1 << 20
MEDIA_TYPE_ITUNES_U = 1 << 21
MEDIA_TYPE_EPUB_BOOK = 1 << 22
MEDIA_TYPE_PDF_BOOK = 1 << 23
MEDIA_TYPE_VIDEO_MASK = MEDIA_TYPE_VIDEO | MEDIA_TYPE_MUSIC_VIDEO | MEDIA_TYPE_TV_SHOW

MEDIA_TYPE_MAP: dict[int, str] = {
    MEDIA_TYPE_AUDIO_VIDEO: "Audio/Video",
    MEDIA_TYPE_AUDIO: "Audio",
    MEDIA_TYPE_VIDEO: "Video",
    MEDIA_TYPE_PODCAST: "Podcast",
    MEDIA_TYPE_VIDEO_PODCAST: "Video Podcast",
    MEDIA_TYPE_AUDIOBOOK: "Audiobook",
    MEDIA_TYPE_MUSIC_VIDEO: "Music Video",
    MEDIA_TYPE_TV_SHOW: "TV Show",
    MEDIA_TYPE_TV_SHOW_ALT: "TV Show (alt)",
    MEDIA_TYPE_RINGTONE: "Ringtone",
    MEDIA_TYPE_RENTAL: "Rental",
    MEDIA_TYPE_ITUNES_EXTRA: "iTunes Extra",
    MEDIA_TYPE_MEMO: "Memo",
    MEDIA_TYPE_ITUNES_U: "iTunes U",
    MEDIA_TYPE_EPUB_BOOK: "EPUB Book",
    MEDIA_TYPE_PDF_BOOK: "PDF Book",
}

# ── playlist sort orders (MHYP +0x2C) ───────────────────────────────

PLAYLIST_SORT_ORDER_MAP: dict[int, str] = {
    0: "default (unset)", 1: "playlist order (manual)", 3: "title", 4: "album", 5: "artist",
    6: "bitrate", 7: "genre", 8: "kind", 9: "date modified", 10: "track number", 11: "size",
    12: "time", 13: "year", 14: "sample rate", 15: "comment", 16: "date added", 17: "equalizer",
    18: "composer", 20: "play count", 21: "last played", 22: "disc number", 23: "my rating",
    24: "release date", 25: "BPM", 26: "grouping", 27: "category", 28: "description",
}

EXPLICIT_FLAG_MAP: dict[int, str] = {0: "none", 1: "explicit", 2: "clean"}

# ── container formats (MHIT +0x0C is a big-endian four-character code) ──


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


FILETYPE_CODES: dict[str, int] = {
    name: _fourcc(code)
    for name, code in (
        ("mp3", "MP3 "), ("m4a", "M4A "), ("m4p", "M4P "), ("m4b", "M4B "), ("m4v", "M4V "),
        ("mp4", "MP4 "), ("wav", "WAV "), ("aif", "AIFF"), ("aiff", "AIFF"), ("aac", "AAC "),
    )
}

# MHIT audio_format_flag: 0 for PCM containers, 1 for audiobooks, 0xFFFF otherwise.
AUDIO_FORMAT_FLAG_DEFAULT = 0xFFFF
AUDIO_FORMAT_FLAG_MAP: dict[str, int] = {"wav": 0, "aif": 0, "aiff": 0, "m4b": 1}
