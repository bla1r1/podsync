# Chapter 3 — `podsync.itunesdb_shared`: shared iTunesDB constants, field definitions, and playlist/device helpers

This chapter is the **canonical reference** for:

- on-disk layout of every iTunesDB chunk type handled by the package (offsets, sizes, defaults, four-byte tags),
- the declarative field read/write engine,
- string/data-object (MHOD) classification and body layouts,
- playlist kind/hierarchy/property/lifecycle rules,
- album identity matching,
- device clock (Mac epoch) conversion.

Everything below is normative. Unless a table says otherwise, **all multi-byte
integers are little-endian** (`struct` format prefixes `<`). The two documented
exceptions are the smart-playlist `SLst` blob and the chapter-data atom tree,
which are **big-endian** (see §5.6 and §5.10).

---

## 0. Package layout, imports, and encoding conventions

### 0.1 Modules

The package `podsync.itunesdb_shared` contains exactly these modules:

| Module | Role |
|---|---|
| `podsync.itunesdb_shared.__init__` | Star-re-exports defs/constants and assembles `FIELD_REGISTRY` |
| `podsync.itunesdb_shared.constants` | Chunk tags, version map, MHOD type names, media/sort/explicit maps, filetype codes |
| `podsync.itunesdb_shared.field_base` | `FieldDef`, exceptions, transforms/validators, generic header, read/write primitives |
| `podsync.itunesdb_shared.extraction` | Flatteners that turn nested parsed chunks into flat dicts |
| `podsync.itunesdb_shared.mhbd_defs` | Database header (MHBD) fields |
| `podsync.itunesdb_shared.mhsd_defs` | Dataset header (MHSD) fields |
| `podsync.itunesdb_shared.mhit_defs` | Track item (MHIT) fields |
| `podsync.itunesdb_shared.mhod_defs` | Data object (MHOD) header fields, body layouts, SPL maps |
| `podsync.itunesdb_shared.mhip_defs` | Playlist item (MHIP) fields |
| `podsync.itunesdb_shared.mhii_defs` | Artist item (MHII) fields |
| `podsync.itunesdb_shared.mhia_defs` | Album item (MHIA) fields |
| `podsync.itunesdb_shared.mhyp_defs` | Playlist (MHYP) fields |
| `podsync.itunesdb_shared.playlist_kinds` | Playlist kind-bit classification |
| `podsync.itunesdb_shared.playlist_hierarchy` | Folder/parent reconciliation |
| `podsync.itunesdb_shared.playlist_properties` | MHOD-55 plist + playlist description lifecycle |
| `podsync.itunesdb_shared.playlist_lifecycle` | Playlist edit-payload overlay rules |
| `podsync.itunesdb_shared.album_identity` | Album equality and grouping |
| `podsync.itunesdb_shared.device_time` | Mac-epoch clock contexts and device timezone discovery |

Dependencies are **stdlib only** (`struct`, `plistlib`, `base64`, `dataclasses`,
`contextvars`, `zoneinfo`, `datetime`, `time`, `math`, `pathlib`) plus sibling
modules inside this package. See §13 for explicit exclusions.

### 0.2 `podsync.itunesdb_shared.__init__` behavior

1. Imports `field_base` (private alias).
2. Star-imports, in this order: `constants`, `extraction`, `field_base`,
   `mhbd_defs`, `mhia_defs`, `mhii_defs`, `mhip_defs`, `mhit_defs`,
   `mhod_defs`, `mhsd_defs`, `mhyp_defs`.
   None of these modules define `__all__`, so every public (non-underscore)
   name of each module becomes an attribute of the package
   (e.g. `podsync.itunesdb_shared.MHIT_HEADER_SIZE`,
   `podsync.itunesdb_shared.write_mhod_header`,
   `podsync.itunesdb_shared.SPL_FIELD_MAP`).
3. Populates `field_base.FIELD_REGISTRY` via a single `.update()` with these
   keys, inserted in exactly this order:
   `"mhbd"`, `"mhit"`, `"mhsd"`, `"mhia"`, `"mhii"`, `"mhip"`, `"mhyp"`,
   `"mhod"`.
   There is **no** registry entry for list containers (`mhlt`, `mhlp`,
   `mhla`, `mhli`) — they only have generic 92-byte headers (§1).

Not star-imported by `__init__`: `playlist_kinds`, `playlist_hierarchy`,
`playlist_properties`, `playlist_lifecycle`, `album_identity`,
`device_time`. Import them by their full module path. (`device_time` and
`playlist_properties` are also pulled in transitively by `field_base` and
`extraction`, respectively, so they are importable as package attributes after
any package import; do not rely on that — use explicit imports.)

### 0.3 Generic chunk header (applies to every chunk)

| Offset | Size | Type | Meaning |
|---|---|---|---|
| `0x00` | 4 | ASCII | Four-byte tag (`mhbd`, `mhsd`, `mhlt`, `mhit`, `mhlp`, `mhla`, `mhli`, `mhyp`, `mhod`, `mhip`, `mhia`, `mhii`) |
| `0x04` | 4 | u32 LE | `header_length` — bytes of header before the first child/body |
| `0x08` | 4 | u32 LE | Total length for item chunks; **child count** for list chunks |

Struct: `"<4sII"` (`GENERIC_HEADER_STRUCT`), size `GENERIC_HEADER_SIZE = 12`.

Header-size constants (all in `field_base` unless noted):

| Constant | Value |
|---|---|
| `MHLT_HEADER_SIZE` | 92 |
| `MHLA_HEADER_SIZE` | 92 |
| `MHLI_HEADER_SIZE` | 92 |
| `MHLP_HEADER_SIZE` | 92 |
| `MHSD_HEADER_SIZE` (`mhsd_defs`) | 96 |
| `MHBD_HEADER_SIZE` (`mhbd_defs`) | 244 (0xF4) |
| `MHIT_HEADER_SIZE` (`mhit_defs`) | 624 (0x270) |
| `MHOD_HEADER_SIZE` (`mhod_defs`) | 24 |
| `MHIP_HEADER_SIZE` (`mhip_defs`) | 76 |
| `MHII_HEADER_SIZE` (`mhii_defs`) | 80 |
| `MHIA_HEADER_SIZE` (`mhia_defs`) | 88 |
| `MHYP_HEADER_SIZE` (`mhyp_defs`) | 184 |

Simple list containers (`mhlt`, `mhla`, `mhli`, `mhlp`) carry **only** the
generic header padded to 92 bytes; bytes `0x0C..0x5B` are zero and the child
count lives at `0x08`.

### 0.4 String encodings (format rules used by this package)

| Kind | Tag/types | Encoding | Length rule | Terminator rule |
|---|---|---|---|---|
| Standard string MHOD | types in `STRING_MHOD_TYPES` (§5.2) | sub-header encoding field `1` or `0` → **UTF-16LE**; `2` → **UTF-8** | sub-header `+0x1C` = byte count of string data; data starts at chunk `+0x28` | No implicit terminator is written or required. The declared byte count is authoritative; readers must slice exactly that many bytes and must not scan for NUL. Disk images from other tools may include a trailing NUL *inside* the declared length — tolerate it, never depend on it. |
| Podcast URL MHOD | types 15, 16 | **UTF-8/ASCII**, no sub-header | body length = `chunk_length − header_length`, body starts immediately after the 24-byte header | Trailing NUL bytes in the body are stripped after decode; a zero-length body decodes to `""` |
| Podcast-URL/`mhod` unknown types | any type not classified | — | — | unknown types are left as stubs by the parser (other chapter) |

SLst string rule payloads are **UTF-16BE** (big-endian, consistent with the
SLst blob — §5.6). Chapter-data titles inside `name` atoms are UTF-16BE as
well (§5.10).

---

## 1. `constants` — chunk tags, versions, and value maps

### 1.1 `chunk_type_map: dict[int, str]`

Maps an MHSD `dataset_type` to the result key used when flattening datasets
(see `extract_datasets`, §6.1):

| dataset_type | key | Meaning |
|---|---|---|
| 1 | `"mhlt"` | Track list (MHIT children) |
| 2 | `"mhlp"` | Playlist list (MHYP children) |
| 3 | `"mhlp_podcast"` | Podcast-aware playlist list (MHIP group-header rows). Format note: type 3 MHSD must appear between type 1 and type 2 for correct podcast listing on device. |
| 4 | `"mhla"` | Album list (MHIA children; iTunes 7.1+) |
| 5 | `"mhlp_smart"` | Smart/category playlist list |
| 6 | `"mhsd_type_6"` | Empty mhlt stub |
| 7 | `"mhsd_type_7"` | Reserved, rarely seen |
| 8 | `"mhsd_type_8"` | Artist list (mhli with mhii children, MHOD type 300) |
| 9 | `"mhsd_type_9"` | Genius list |
| 10 | `"mhsd_type_10"` | Empty mhlt stub |

Any `dataset_type` not in the map is skipped (no key emitted, no exception).

### 1.2 `version_map: dict[int, str]` and `get_version_name`

| Key | Name | Key | Name | Key | Name |
|---|---|---|---|---|---|
| 0x01 | iTunes 1.0 | 0x02 | iTunes 2.0 | 0x03 | iTunes 3.0 |
| 0x04 | iTunes 4.0 | 0x05 | iTunes 4.0.1 | 0x06 | iTunes 4.1 |
| 0x07 | iTunes 4.1.1 | 0x08 | iTunes 4.1.2 | 0x09 | iTunes 4.2 |
| 0x0a | iTunes 4.5 | 0x0b | iTunes 4.7 | 0x0c | iTunes 4.71/4.8 |
| 0x0d | iTunes 4.9 | 0x0e | iTunes 5 | 0x0f | iTunes 6 |
| 0x10 | iTunes 6.0.1 | 0x11 | iTunes 6.0.2-6.0.4 | 0x12 | iTunes 6.0.5 |
| 0x13 | iTunes 7.0 | 0x14 | iTunes 7.1 | 0x15 | iTunes 7.2 |
| 0x16 | Unknown (0x16) | 0x17 | iTunes 7.3.0 | 0x18 | iTunes 7.3.1-7.3.2 |
| 0x19 | iTunes 7.4 | 0x1a | iTunes 7.4.1 | 0x1b | iTunes 7.4.2 |
| 0x1c | iTunes 7.5 | 0x1d | iTunes 7.6 | 0x1e | iTunes 7.7 |
| 0x1f | iTunes 8.0 | 0x20 | iTunes 8.0.1 | 0x21 | iTunes 8.0.2 |
| 0x22 | iTunes 8.1 | 0x23 | iTunes 8.1.1 | 0x24 | iTunes 8.2 |
| 0x25 | iTunes 8.2.1 | 0x26 | iTunes 9.0 | 0x27 | iTunes 9.0.1 |
| 0x28 | iTunes 9.0.2 | 0x29 | iTunes 9.0.3 | 0x2a | iTunes 9.1 |
| 0x2b | iTunes 9.1.1 | 0x2c | iTunes 9.2 | 0x2d | iTunes 9.2.1 |
| 0x30 | iTunes 9.2+ | 0x40 | iTunes 10.x | 0x50 | iTunes 11.x |
| 0x60 | iTunes 12.x | 0x70 | iTunes 12.5+ | 0x75 | iTunes 12.9+ |

Note the deliberate gaps (0x2e–0x2f, 0x31–0x3f, 0x41–0x4f, 0x51–0x5f,
0x61–0x6f, 0x71–0x74, 0x76+): they are **absent** from the map and resolve via
the "closest lower" rule below.

`get_version_name(version_hex: int | str) -> str`:

1. If `str`: if it starts with `'0x'` → `int(s, 16)`, otherwise → `int(s)`
   (decimal; invalid strings propagate `ValueError`).
2. Exact key in `version_map` → that name.
3. Else, collect all map keys `v <= version_hex`; if any, return
   `f"{version_map[max(keys)]} (or newer)"`.
4. Else return `f"Unknown (version {hex(version_hex)})"`.

Examples: `0x2E` → `"iTunes 9.2.1 (or newer)"`; `0x31` → `"iTunes 9.2+ (or newer)"`;
`0` → `"Unknown (version 0x0)"`.

### 1.3 `identifier_readable_map: dict[str, str]`

`"mhbd"`→Database, `"mhsd"`→Dataset, `"mhlt"`→Track List,
`"mhlp"`→Playlist or Podcast List, `"mhla"`→Album List, `"mhli"`→Artist List,
`"mhlp_smart"`→Smart Playlist List, `"mhia"`→Album Item, `"mhii"`→Artist Item,
`"mhit"`→Track Item, `"mhyp"`→Playlist, `"mhod"`→Data Object,
`"mhip"`→Playlist Item.

### 1.4 `mhod_type_map: dict[int, str]` (complete)

| ID | Name | ID | Name | ID | Name |
|---|---|---|---|---|---|
| 1 | Title | 2 | Location | 3 | Album |
| 4 | Artist | 5 | Genre | 6 | Filetype |
| 7 | eq_setting | 8 | Comment | 9 | Category |
| 10 | Lyrics | 12 | Composer | 13 | Grouping |
| 14 | Description Text | 15 | Podcast Enclosure URL | 16 | Podcast RSS URL |
| 17 | Chapter Data | 18 | Subtitle | 19 | Show |
| 20 | Episode | 21 | TV Network | 22 | Album Artist |
| 23 | Sort Artist | 24 | Track Keywords | 25 | Show Locale |
| 26 | iTunes Store Asset Info | 27 | Sort Title | 28 | Sort Album |
| 29 | Sort Album Artist | 30 | Sort Composer | 31 | Sort Show |
| 32 | Unknown for Video Track | 33 | Unknown (33) | 34 | Unknown (34) |
| 35 | Unknown (35) | 36 | Unknown (36) | 37 | Content Provider |
| 38 | Unknown (38) | 39 | Copyright | 40 | Unknown (40) |
| 41 | Unknown (41) | 42 | Encoding Quality Descriptor | 43 | Purchase Account |
| 44 | Purchaser Name | 50 | Smart Playlist Data | 51 | Smart Playlist Rules |
| 52 | Library Playlist Index | 53 | Library Playlist Jump Table | 55 | Playlist Property Plist |
| 100 | Column Size or Playlist Order | 102 | Playlist Settings (binary) | 200 | Album (Used by Album Item) |
| 201 | Artist (Used by Album Item) | 202 | Sort Artist (Used by Album Item) | 203 | Podcast URL (Used by Album Item) |
| 204 | Show (Used by Album Item) | 300 | Artist (Used by Artist Item) | | |

**Unmapped IDs** (skipped by name-based helpers): 11, 45–49, 54, 56–99,
101, 103–199, 205–299, 301+.

Body-format grouping (normative for parsers/writers):

- Types 1–14, 18–31, 33–44: track string MHODs with the standard sub-header.
- Types 15–16: podcast URLs, UTF-8, **no** sub-header.
- Type 17: chapter data, big-endian atom tree.
- Type 32: opaque binary video descriptor (not a string).
- Types 50, 51, 52, 53, 55, 100, 102: dedicated binary bodies (§5).
- Types 200–204: album-item strings; type 300: artist-item string.

### 1.5 Media type bitmask (MHIT `0xD0`)

`MEDIA_TYPE_MAP: dict[int, str]`:

| Value | Name | Value | Name |
|---|---|---|---|
| 0x00000000 | Audio/Video | 0x00000001 | Audio |
| 0x00000002 | Video | 0x00000004 | Podcast |
| 0x00000006 | Video Podcast | 0x00000008 | Audiobook |
| 0x00000020 | Music Video | 0x00000040 | TV Show |
| 0x00000060 | TV Show (alt) | 0x00004000 | Ringtone |
| 0x00008000 | Rental | 0x00010000 | iTunes Extra |
| 0x00100000 | Memo | 0x00200000 | iTunes U |
| 0x00400000 | EPUB Book | 0x00800000 | PDF Book |

Integer constants (same values):

```
MEDIA_TYPE_AUDIO_VIDEO = 0x00     MEDIA_TYPE_AUDIO = 0x01
MEDIA_TYPE_VIDEO = 0x02           MEDIA_TYPE_PODCAST = 0x04
MEDIA_TYPE_VIDEO_PODCAST = 0x06   MEDIA_TYPE_AUDIOBOOK = 0x08
MEDIA_TYPE_MUSIC_VIDEO = 0x20     MEDIA_TYPE_TV_SHOW = 0x40
MEDIA_TYPE_TV_SHOW_ALT = 0x60     MEDIA_TYPE_RINGTONE = 0x4000
MEDIA_TYPE_RENTAL = 0x8000        MEDIA_TYPE_ITUNES_EXTRA = 0x10000
MEDIA_TYPE_MEMO = 0x100000        MEDIA_TYPE_ITUNES_U = 0x200000
MEDIA_TYPE_EPUB_BOOK = 0x400000   MEDIA_TYPE_PDF_BOOK = 0x800000
MEDIA_TYPE_VIDEO_MASK = 0x02 | 0x20 | 0x40   (= 0x62)
```

### 1.6 Playlist sort order (MHYP `0x2C`)

`PLAYLIST_SORT_ORDER_MAP`:

| ID | Name | ID | Name |
|---|---|---|---|
| 0 | default (unset) | 1 | playlist order (manual) |
| 3 | title | 4 | album |
| 5 | artist | 6 | bitrate |
| 7 | genre | 8 | kind |
| 9 | date modified | 10 | track number |
| 11 | size | 12 | time |
| 13 | year | 14 | sample rate |
| 15 | comment | 16 | date added |
| 17 | equalizer | 18 | composer |
| 20 | play count | 21 | last played |
| 22 | disc number | 23 | my rating |
| 24 | release date | 25 | BPM |
| 26 | grouping | 27 | category |
| 28 | description | | |

IDs 2 and 19 are intentionally absent.

### 1.7 Explicit flag (MHIT `0x92`)

`EXPLICIT_FLAG_MAP = {0: "none", 1: "explicit", 2: "clean"}`.

### 1.8 Named MHOD type constants

`MHOD_TYPE_TITLE=1`, `_LOCATION=2`, `_ALBUM=3`, `_ARTIST=4`, `_GENRE=5`,
`_FILETYPE=6`, `_EQ_SETTING=7`, `_COMMENT=8`, `_CATEGORY=9`, `_LYRICS=10`,
`_COMPOSER=12`, `_GROUPING=13`, `_DESCRIPTION=14`,
`_PODCAST_ENCLOSURE_URL=15`, `_PODCAST_RSS_URL=16`, `_CHAPTER_DATA=17`,
`_SUBTITLE=18`, `_SHOW_NAME=19`, `_EPISODE_ID=20`, `_NETWORK_NAME=21`,
`_ALBUM_ARTIST=22`, `_SORT_ARTIST=23`, `_KEYWORDS=24`, `_SHOW_LOCALE=25`,
`_SORT_NAME=27`, `_SORT_ALBUM=28`, `_SORT_ALBUM_ARTIST=29`,
`_SORT_COMPOSER=30`, `_SORT_SHOW=31`, `_SMART_PLAYLIST_DATA=50`,
`_SMART_PLAYLIST_RULES=51`, `_LIBRARY_PLAYLIST_INDEX=52`,
`_LIBRARY_PLAYLIST_JUMP_TABLE=53`, `_PLAYLIST_PROPERTY_PLIST=55`,
`_COLUMN_SIZE_OR_ORDER=100`, `_PLAYLIST_SETTINGS=102`,
`_ALBUM_ALBUM=200`, `_ALBUM_ARTIST_ITEM=201`, `_ALBUM_SORT_ARTIST=202`,
`_ALBUM_PODCAST_URL=203`, `_ALBUM_SHOW=204`, `_ARTIST_NAME=300`.
(Prefix is `MHOD_TYPE_`. There are **no** named constants for IDs 11, 26,
32–44, 54, 101, etc., even where `mhod_type_map` has names.)

### 1.9 Filetype codes and audio-format flags

`FILETYPE_CODES: dict[str, int]` — big-endian ASCII packed as LE u32:

| Key | Value | Bytes | Key | Value | Bytes |
|---|---|---|---|---|---|
| `mp3` | 0x4D503320 | `"MP3 "` | `m4a` | 0x4D344120 | `"M4A "` |
| `m4p` | 0x4D345020 | `"M4P "` | `m4b` | 0x4D344220 | `"M4B "` |
| `m4v` | 0x4D345620 | `"M4V "` | `mp4` | 0x4D503420 | `"MP4 "` |
| `wav` | 0x57415620 | `"WAV "` | `aif` | 0x41494646 | `"AIFF"` |
| `aiff` | 0x41494646 | `"AIFF"` | `aac` | 0x41414320 | `"AAC "` |

`AUDIO_FORMAT_FLAG_MAP` (MHIT `0x7E` codec hint): `wav/aif/aiff → 0x0000`
(lossless), `m4b → 0x0001` (Audible-style audiobook); every other filetype →
`AUDIO_FORMAT_FLAG_DEFAULT = 0xFFFF`.

---

## 2. `field_base` — the field definition and read/write engine

### 2.1 `FieldDef` dataclass

`@dataclass(frozen=True, slots=True) class FieldDef` with attributes:

| Attribute | Type | Meaning |
|---|---|---|
| `name` | `str` | Canonical snake_case key shared by parser and writer dicts |
| `offset` | `int` | Byte offset from chunk start |
| `size` | `int` | Byte width |
| `struct_format` | `str` | `struct` format, e.g. `'<I'`, `'B'`, `'<8s'` |
| `read_transform` | `Callable \| None` | Applied after unpack on read |
| `write_transform` | `Callable \| None` | Applied before validation/pack on write |
| `default` | `Any = 0` | Used when value absent (read fallback: header too small) |
| `validator` | `Callable \| None` | Raises `ValueError` on invalid value |
| `min_header_length` | `int \| None = None` | Field exists only when `header_length >=` this |
| `required` | `bool = False` | Missing key on write → `MissingRequiredFieldError` |
| `section_type` | `str = ""` | Parent tag (`'mhit'`, `'mhbd'`, …) for error messages |

Factory helpers (private by name but part of the layout contract; used by every
`*_defs` module): `_u32(name, offset, **kw)` → `'<I'` size 4;
`_i32` → `'<i'`; `_u16` → `'<H'` size 2; `_u64` → `'<Q'` size 8;
`_u8` → `'B'` size 1; `_f32` → `'<f'`;
`_raw(name, offset, size, **kw)` → `f'<{size}s'` with default
`b"\x00" * size` unless overridden.

### 2.2 Exceptions

| Class | Base | Message / attributes |
|---|---|---|
| `WriteError` | `Exception` | Base for all write-time errors |
| `MissingRequiredFieldError(WriteError)` | | `f"Required field '{field_name}' missing for section '{section_type}'"`; attrs `.section_type`, `.field_name` |
| `InvalidFieldValueError(WriteError)` | | `f"Invalid value for '{field_name}' in section '{section_type}': {detail}"`; attrs `.section_type`, `.field_name` |

Also re-exported/defined here: `MAC_EPOCH_OFFSET = 2_082_844_800`
(= `device_time.MAC_EPOCH_OFFSET`, seconds between 1904-01-01 and 1970-01-01)
and `_U32_MAX = MAC_U32_MAX = 0xFFFF_FFFF` (private).

### 2.3 Transform / validator functions (exact behavior)

| Function | Behavior |
|---|---|
| `_int_or_default(value, default=0)` | Non-finite `float` (NaN/inf) → default; `TypeError/ValueError/OverflowError` → default; else `int(value)` |
| `mac_to_unix(mac_ts)` | `current_device_time_context().mac_to_unix(mac_ts)` (§12) |
| `unix_to_mac(unix_ts)` | `current_device_time_context().unix_to_mac(_int_or_default(unix_ts))`; may raise `MacTimestampOutOfRangeError` |
| `sample_rate_to_fixed(hz)` | Clamp `hz` via `_int_or_default` to `[0, 0xFFFF]`, return `hz << 16` (16.16 fixed point for MHIT `0x3C`) |
| `fixed_to_sample_rate(raw)` | `raw >> 16` |
| `validate_rating(value)` | Coerce with default `-1`; raise `ValueError(f"rating {value} outside 0-100")` unless `0 <= v <= 100` |
| `clamp_rating(value)` | Coerce with default 0; return `max(0, min(100, v))` |
| `validate_volume(value)` | Coerce with default `-1000`; raise `ValueError(f"volume {value} outside -255..+255")` unless `-255 <= v <= 255` |
| `filetype_to_string(val)` | Non-`int` or `val <= 0` → `""`; else `val.to_bytes(4, "big").decode("ascii").rstrip("\x00").strip()`; `OverflowError/UnicodeDecodeError` → `str(val)` |
| `strip_article(name)` | Empty → unchanged; if `name.lower()` starts with `'the '`, `'a '`, or `'an '` (checked in that order), drop exactly that prefix (remainder keeps original case); else unchanged |

### 2.4 Registry and lookup

- `FIELD_REGISTRY: dict[str, list[FieldDef]]` — module-level dict, populated
  by `__init__` (§0.2). Mutated via `.update()` (never re-bound).
- `get_fields(section_type: str, header_length: int | None = None) -> list[FieldDef]`
  — returns a **copy** of the registry list; if `header_length` given, drops
  fields with `min_header_length is not None and header_length < min`.
  Unknown section → `[]`.

### 2.5 Read primitives

`read_field(data, base_offset, field, header_length=None) -> Any`:

1. If `field.min_header_length` is set and (`header_length is None` or
   `header_length < min`) → return `field.default` immediately.
2. `struct.unpack_from(field.struct_format, data, base_offset + field.offset)[0]`.
3. Apply `read_transform(raw)` if present; else return raw.

`read_fields(data, base_offset, section_type, header_length=None) -> dict[str, Any]`:

- Iterates `FIELD_REGISTRY.get(section_type, [])` **in list order** and returns
  `{field.name: value}` for **every** registered field (extended fields whose
  `min_header_length` exceeds `header_length` appear with their defaults — the
  key is always present).

### 2.6 Write primitives

`write_field(buffer, base_offset, field, value, section_type="") -> None`,
in this exact order:

1. `value = field.write_transform(value)` if a transform exists.
2. If a validator exists: call it; catch `ValueError`/`TypeError` and raise
   `InvalidFieldValueError(section_type or field.section_type, field.name, str(exc))`.
3. Type coercion by the **last character** of `struct_format`:
   - Integer codes `I i H h Q q B b N P`: `int(value)` if not already `int`.
     Then clamp for codes present in this table (N and P are coerced but not
     clamped):

       | Code | Range |
       |---|---|
       | `B` | 0 … 0xFF |
       | `H` | 0 … 0xFFFF |
       | `I` | 0 … 0xFFFF_FFFF |
       | `Q` | 0 … 0xFFFF_FFFF_FFFF_FFFF |
       | `b` | −0x80 … 0x7F |
       | `h` | −0x8000 … 0x7FFF |
       | `i` | −0x8000_0000 … 0x7FFF_FFFF |
       | `q` | −0x8000_0000_0000_0000 … 0x7FFF_FFFF_FFFF_FFFF |
   - Float codes `f d`: `float(value)` if not already `float`.
   - Other codes (`s`, etc.): no coercion.
4. `struct.pack_into(...)`; a `struct.error` is re-raised as `struct.error`
   with message
   `f"Failed to pack field '{field.name}' (format={field.struct_format!r}, value={value!r} type={type(value).__name__}): {exc}"`.

`write_fields(buffer, base_offset, section_type, values, header_length) -> None`:

- Iterate the registry in stored (offset-ascending) order.
- Skip fields where `min_header_length is not None and header_length < min`.
- Value selection: `values[field.name]` if the key exists (even if value is
  `None`/`0`), else if `field.required` raise
  `MissingRequiredFieldError(section_type, field.name)`, else `field.default`.
- Delegate each to `write_field`.
- The buffer must already be at least `header_length` bytes at the chunk
  region; this function never resizes.

### 2.7 Generic/list header builders

| Function | Contract |
|---|---|
| `write_generic_header(buffer, offset, tag: bytes, header_length: int, total_length_or_count: int) -> None` | Packs `"<4sII"` at `offset`: tag, `header_length`, value for `+0x08` (total length for items, child count for lists) |
| `write_list_header(tag: bytes, header_length: int, child_count: int) -> bytes` | `bytearray(header_length)` (zero-filled), generic header at 0; returns bytes of exactly `header_length` |
| `write_list_chunk(tag: bytes, header_length: int, child_chunks: Iterable[bytes]) -> bytes` | `write_list_header(tag, header_length, len(children)) + b"".join(children)`; children are materialized once (`tuple`) |

Smoke-test expectations: `write_list_header(b"mhlt", 92, 0)` has length 92,
unpacks as `(b"mhlt", 92, 0)` with bytes `12..91` all zero;
`write_list_chunk(b"mhlp", 92, [b"one", b"two"])` unpacks as
`(b"mhlp", 92, 2)` with body `b"onetwo"`.

---

## 3. Per-chunk field tables (the `*_defs` modules)

Column meanings: **Off** = offset from chunk start; **Size** = bytes;
**Type** = struct kind (`u32/u16/u8/u64/i32/f32/rawN`); **Def** = default used
when absent (raw defaults are N zero bytes; unspecified integer default is 0);
**Min** = `min_header_length` (`—` = always present); **Req** = `required`;
**Notes** = read/write transforms.

### 3.1 MHBD — database header (`mhbd_defs`)

Named offset constants (used by hash/signing code for zero-before-sign):

| Constant | Value | Width |
|---|---|---|
| `MHBD_OFFSET_DB_ID` | 0x18 | 8 (u64) |
| `MHBD_OFFSET_HASHING_SCHEME` | 0x30 | 2 (u16) |
| `MHBD_OFFSET_UNK_0x32` | 0x32 | 20 (raw) |
| `MHBD_OFFSET_HASH58` | 0x58 | 20 (raw) |
| `MHBD_OFFSET_HASH72` | 0x72 | 46 (raw) |
| `MHBD_OFFSET_HASHAB` | 0xAB | 57 (raw) |

`MHBD_FIELDS` (writer header size `MHBD_HEADER_SIZE = 244`):

| Field | Off | Size | Type | Def | Min | Req | Notes |
|---|---|---|---|---|---|---|---|
| compressed | 0x0C | 4 | u32 | 1 | — | | |
| version | 0x10 | 4 | u32 | 0 | — | ✔ | db version (§1.2) |
| child_count | 0x14 | 4 | u32 | 0 | — | | |
| db_id | 0x18 | 8 | u64 | 0 | — | ✔ | |
| platform | 0x20 | 2 | u16 | 2 | — | | |
| unk0x22 | 0x22 | 2 | u16 | 0 | — | | preserve verbatim (observed 611) |
| db_id_2 | 0x24 | 8 | u64 | 0 | — | | |
| unk0x2c | 0x2C | 4 | u32 | 0 | — | | |
| hashing_scheme | 0x30 | 2 | u16 | 0 | — | | |
| unk0x32 | 0x32 | 20 | raw | zeros | — | | |
| language | 0x46 | 2 | raw | `b"en"` | — | | |
| db_persistent_id | 0x48 | 8 | u64 | 0 | — | | |
| unk0x50 | 0x50 | 4 | u32 | 1 | — | | |
| unk0x54 | 0x54 | 4 | u32 | 15 | — | | |
| hash58 | 0x58 | 20 | raw | zeros | — | | |
| timezone_offset | 0x6C | 4 | i32 | 0 | — | | device UTC offset in seconds at last write |
| hash_type_indicator | 0x70 | 2 | u16 | 0 | — | | |
| hash72 | 0x72 | 46 | raw | zeros | — | | |
| audio_language | 0xA0 | 2 | u16 | 0 | 0xA2 | | extended |
| subtitle_language | 0xA2 | 2 | u16 | 0 | 0xA4 | | extended |
| unk0xa4 | 0xA4 | 2 | u16 | 0 | 0xA6 | | extended |
| unk0xa6 | 0xA6 | 2 | u16 | 0 | 0xA8 | | extended |
| cdb_flag | 0xA8 | 2 | u16 | 0 | 0xAA | | extended |
| hashab | 0xAB | 57 | raw | zeros | 0xE4 | | reserved HASHAB region; preserve |

### 3.2 MHSD — dataset header (`mhsd_defs`)

`MHSD_HEADER_SIZE = 96`.

| Field | Off | Size | Type | Def | Min | Req | Notes |
|---|---|---|---|---|---|---|---|
| dataset_type | 0x0C | 4 | u32 | 0 | — | ✔ | key into `chunk_type_map` (§1.1) |

### 3.3 MHIT — track item (`mhit_defs`)

`MHIT_HEADER_SIZE = 0x270` (624).

`mhit_header_size_for_version(db_version: int) -> int`:

| Condition (inclusive) | Return |
|---|---|
| `db_version <= 0x12` | `0x9C` (156, pre-iTunes 7 minimum) |
| `db_version <= 0x19` | `0x148` (328) |
| `db_version <= 0x2D` | `0x1F8` (504) |
| otherwise | `0x270` (624) |

Core fields (present for every header ≥ 0x9C):

| Field | Off | Size | Type | Def | Req | Notes |
|---|---|---|---|---|---|---|
| child_count | 0x0C | 4 | u32 | 0 | | MHOD child count |
| track_id | 0x10 | 4 | u32 | 0 | ✔ | |
| visible | 0x14 | 4 | u32 | 1 | | |
| filetype | 0x18 | 4 | u32 | 0 | | u32 file code (§1.9) |
| vbr_flag | 0x1C | 1 | u8 | 0 | | |
| mp3_flag | 0x1D | 1 | u8 | 0 | | |
| compilation_flag | 0x1E | 1 | u8 | 0 | | |
| rating | 0x1F | 1 | u8 | 0 | | write: `clamp_rating`; validator: `validate_rating` |
| last_modified | 0x20 | 4 | u32 | 0 | | read `mac_to_unix` / write `unix_to_mac` |
| size | 0x24 | 4 | u32 | 0 | | bytes |
| length | 0x28 | 4 | u32 | 0 | | ms |
| track_number | 0x2C | 4 | u32 | 0 | | |
| total_tracks | 0x30 | 4 | u32 | 0 | | |
| year | 0x34 | 4 | u32 | 0 | | |
| bitrate | 0x38 | 4 | u32 | 0 | | |
| sample_rate_1 | 0x3C | 4 | u32 | 0 | | 16.16 fixed: read `raw>>16`, write `hz<<16` |
| volume | 0x40 | 4 | i32 | 0 | | validator `validate_volume` (−255..255) |
| start_time | 0x44 | 4 | u32 | 0 | | |
| stop_time | 0x48 | 4 | u32 | 0 | | |
| sound_check | 0x4C | 4 | u32 | 0 | | |
| play_count_1 | 0x50 | 4 | u32 | 0 | | |
| play_count_2 | 0x54 | 4 | u32 | 0 | | |
| last_played | 0x58 | 4 | u32 | 0 | | mac↔unix |
| disc_number | 0x5C | 4 | u32 | 0 | | |
| total_discs | 0x60 | 4 | u32 | 0 | | |
| user_id | 0x64 | 4 | u32 | 0 | | |
| date_added | 0x68 | 4 | u32 | 0 | | mac↔unix |
| bookmark_time | 0x6C | 4 | u32 | 0 | | |
| db_track_id | 0x70 | 8 | u64 | 0 | ✔ | persistent id |
| checked_flag | 0x78 | 1 | u8 | 0 | | |
| app_rating | 0x79 | 1 | u8 | 0 | | |
| bpm | 0x7A | 2 | u16 | 0 | | |
| artwork_count | 0x7C | 2 | u16 | 0 | | |
| audio_format_flag | 0x7E | 2 | u16 | 0xFFFF | | codec hint (§1.9) |
| artwork_size | 0x80 | 4 | u32 | 0 | | |
| unk0x84 | 0x84 | 4 | u32 | 0 | | |
| sample_rate_2 | 0x88 | 4 | f32 | 0.0 | | |
| date_released | 0x8C | 4 | u32 | 0 | | mac↔unix |
| mpeg_audio_type | 0x90 | 2 | u16 | 0 | | |
| explicit_flag | 0x92 | 1 | u8 | 0 | | §1.7 |
| purchased_aac_flag | 0x93 | 1 | u8 | 0 | | |
| unk0x94 | 0x94 | 4 | u32 | 0 | | |
| genius_category_id | 0x98 | 4 | u32 | 0 | | |

Extended fields (guarded by `min_header_length`):

| Field | Off | Size | Type | Def | Min | Notes |
|---|---|---|---|---|---|---|
| skip_count | 0x9C | 4 | u32 | 0 | 0xA0 | |
| last_skipped | 0xA0 | 4 | u32 | 0 | 0xA4 | mac↔unix |
| has_artwork | 0xA4 | 1 | u8 | 0 | 0xA5 | |
| skip_when_shuffling | 0xA5 | 1 | u8 | 0 | 0xA6 | |
| remember_position | 0xA6 | 1 | u8 | 0 | 0xA7 | |
| use_podcast_now_playing_flag | 0xA7 | 1 | u8 | 0 | 0xA8 | |
| db_track_id_2 | 0xA8 | 8 | u64 | 0 | 0xB0 | |
| lyrics_flag | 0xB0 | 1 | u8 | 0 | 0xB1 | |
| movie_flag | 0xB1 | 1 | u8 | 0 | 0xB2 | |
| not_played_flag | 0xB2 | 1 | u8 | 0 | 0xB3 | |
| unk0xB3 | 0xB3 | 1 | u8 | 0 | 0xB4 | |
| unk0xB4 | 0xB4 | 4 | u32 | 0 | 0xB8 | |
| pregap | 0xB8 | 4 | u32 | 0 | 0xBC | |
| sample_count | 0xBC | 8 | u64 | 0 | 0xC4 | |
| unk0xC4 | 0xC4 | 4 | u32 | 0 | 0xC8 | |
| postgap | 0xC8 | 4 | u32 | 0 | 0xCC | |
| encoder | 0xCC | 4 | u32 | 0 | 0xD0 | |
| media_type | 0xD0 | 4 | u32 | 1 | 0xD4 | §1.5 |
| season_number | 0xD4 | 4 | u32 | 0 | 0xD8 | |
| episode_number | 0xD8 | 4 | u32 | 0 | 0xDC | |
| date_added_to_itunes | 0xDC | 4 | u32 | 0 | 0xE0 | mac↔unix |
| store_track_id | 0xE0 | 4 | u32 | 0 | 0xE4 | |
| store_encoder_version | 0xE4 | 4 | u32 | 0 | 0xE8 | |
| store_artist_id | 0xE8 | 4 | u32 | 0 | 0xEC | |
| unk0xEC | 0xEC | 4 | u32 | 0 | 0xF0 | |
| store_album_id | 0xF0 | 4 | u32 | 0 | 0xF4 | |
| store_content_flag | 0xF4 | 4 | u32 | 0 | 0xF8 | |
| gapless_audio_payload_size | 0xF8 | 4 | u32 | 0 | 0xFC | |
| unk0xFC | 0xFC | 4 | u32 | 0 | 0x100 | |
| gapless_track_flag | 0x100 | 2 | u16 | 0 | 0x102 | |
| gapless_album_flag | 0x102 | 2 | u16 | 0 | 0x104 | |
| hash_0x104 | 0x104 | 20 | raw | zeros | 0x118 | |
| unk0x118 | 0x118 | 4 | u32 | 0 | 0x11C | |
| unk0x11C | 0x11C | 4 | u32 | 0 | 0x120 | |
| album_id | 0x120 | 4 | u32 | 0 | 0x124 | MHIA reference |
| db_id_2_ref | 0x124 | 8 | u64 | 0 | 0x12C | |
| size_2 | 0x12C | 4 | u32 | 0 | 0x130 | |
| unk0x130 | 0x130 | 4 | u32 | 0 | 0x134 | |
| sort_mhod_indicators | 0x134 | 8 | raw | zeros | 0x13C | bit 0 ↔ presence of sort MHOD types 27/28/23/29/30/31 |
| unk0x154 | 0x154 | 4 | u32 | 0 | 0x158 | |
| artwork_id_ref | 0x160 | 4 | u32 | 0 | 0x164 | |
| unk0x164 | 0x164 | 4 | u32 | 0 | 0x168 | |
| unk0x168 | 0x168 | 4 | u32 | 1 | 0x16C | |
| unk0x173 | 0x173 | 1 | u8 | 0 | 0x174 | |
| movie_flag_2 | 0x194 | 1 | u8 | 0 | 0x195 | mirrors movie_flag |
| purchased_aac_flag_2 | 0x195 | 1 | u8 | 0 | 0x196 | mirrors purchased_aac_flag |
| unk0x197 | 0x197 | 1 | u8 | 0 | 0x198 | |
| unk0x1A0 | 0x1A0 | 4 | u32 | 0 | 0x1A4 | duplicate of unk0x154 |
| store_track_id_2 | 0x1B0 | 8 | u64 | 0 | 0x1B8 | widened store block (low word mirrors 0xE0) |
| store_encoder_version_2 | 0x1B8 | 8 | u64 | 0 | 0x1C0 | |
| store_artist_id_2 | 0x1C0 | 8 | u64 | 0 | 0x1C8 | |
| unk0xEC_2 | 0x1C8 | 8 | u64 | 0 | 0x1D0 | |
| store_album_id_2 | 0x1D0 | 8 | u64 | 0 | 0x1D8 | |
| store_content_flag_2 | 0x1D8 | 8 | u64 | 0 | 0x1E0 | |
| artist_id_ref | 0x1E0 | 4 | u32 | 0 | 0x1E4 | |
| unk0x1EC | 0x1EC | 4 | u32 | 0 | 0x1F0 | |
| composer_id | 0x1F4 | 4 | u32 | 0 | 0x1F8 | |
| unk0x1F8 | 0x1F8 | 4 | u32 | 0 | 0x1FC | |
| unk0x20C | 0x20C | 4 | u32 | 0 | 0x210 | |
| unk0x229 | 0x229 | 1 | u8 | 0 | 0x22A | |
| unk0x22B | 0x22B | 1 | u8 | 0 | 0x22C | |

Unmapped gaps inside the MHIT header (zero padding; no `FieldDef`):
`0x13C..0x153`, `0x158..0x15F`, `0x16C..0x172`, `0x174..0x193`,
`0x196` (1 byte), `0x198..0x19F`, `0x1A4..0x1AF`, `0x1E4..0x1EB`,
`0x1F0..0x1F3`, `0x1FC..0x20B`, `0x210..0x228`, `0x22A` (1 byte),
and `0x22C..header_length-1` after the last defined field.

### 3.4 MHOD — data object common header (`mhod_defs`)

`MHOD_HEADER_SIZE = 24`.

| Field | Off | Size | Type | Def | Req | Notes |
|---|---|---|---|---|---|---|
| mhod_type | 0x0C | 4 | u32 | 0 | ✔ | §1.4 |
| unk0x10 | 0x10 | 4 | u32 | 0 | | preserved from parser |
| unk0x14 | 0x14 | 4 | u32 | 0 | | preserved from parser |

### 3.5 MHIP — playlist item (`mhip_defs`)

`MHIP_HEADER_SIZE = 76`.

| Field | Off | Size | Type | Def | Min | Req | Notes |
|---|---|---|---|---|---|---|---|
| child_count | 0x0C | 4 | u32 | 0 | — | | |
| podcast_group_flag | 0x10 | 2 | u16 | 0 | — | | `0x0100` marks a dataset-3 group-header row (track_id usually 0) |
| unk0x12 | 0x12 | 2 | u16 | 0 | — | | observed 0x8000/0x8001 only with group flag |
| group_id | 0x14 | 4 | u32 | 0 | — | | |
| track_id | 0x18 | 4 | u32 | 0 | — | ✔ | MHYP/MHIT reference |
| timestamp | 0x1C | 4 | u32 | 0 | — | | mac↔unix |
| group_id_ref | 0x20 | 4 | u32 | 0 | — | | |
| unk0x24_group_persistent_id | 0x24 | 8 | u64 | 0 | — | | non-zero only on podcast group headers |
| track_persistent_id | 0x2C | 8 | u64 | 0 | 0x34 | | |
| mhip_persistent_id | 0x3C | 8 | u64 | 0 | 0x44 | | per-entry persistent id |

### 3.6 MHII — artist item (`mhii_defs`)

`MHII_HEADER_SIZE = 80`.

| Field | Off | Size | Type | Def | Req |
|---|---|---|---|---|---|
| child_count | 0x0C | 4 | u32 | 0 | |
| artist_id | 0x10 | 4 | u32 | 0 | ✔ |
| sql_id | 0x14 | 8 | u64 | 0 | |
| platform_flag | 0x1C | 4 | u32 | 2 | |

### 3.7 MHIA — album item (`mhia_defs`)

`MHIA_HEADER_SIZE = 88`.

| Field | Off | Size | Type | Def | Min | Req | Notes |
|---|---|---|---|---|---|---|---|
| child_count | 0x0C | 4 | u32 | 0 | — | | |
| album_id | 0x10 | 4 | u32 | 0 | — | ✔ | |
| sql_id | 0x14 | 8 | u64 | 0 | — | | |
| platform_flag | 0x1C | 2 | u16 | 2 | — | | observed content classes: 2 music, 3 podcast, 4 TV, 0x102 compilation |
| album_compilation_flag | 0x1E | 2 | u16 | 0 | — | | |
| album_track_db_id | 0x20 | 8 | u64 | 0 | 0x28 | | representative member track's `db_track_id` |
| album_rating | 0x28 | 1 | u8 | 0 | — | | 0–100 scale |
| unk0x29_rating_flag | 0x29 | 1 | u8 | 0 | — | | non-zero exactly when album_rating ≠ 0 |
| season_number | 0x2C | 4 | u32 | 0 | — | | mirrors member tracks' season_number on TV albums |

`+0x2A..+0x2B` are zero padding with no field.

### 3.8 MHYP — playlist (`mhyp_defs`)

`MHYP_HEADER_SIZE = 184`.

| Field | Off | Size | Type | Def | Min | Req | Notes |
|---|---|---|---|---|---|---|---|
| mhod_child_count | 0x0C | 4 | u32 | 0 | — | | |
| mhip_child_count | 0x10 | 4 | u32 | 0 | — | | |
| master_flag | 0x14 | 1 | u8 | 0 | — | | master playlist marker |
| flag1 | 0x15 | 1 | u8 | 0 | — | | |
| flag2 | 0x16 | 1 | u8 | 0 | — | | |
| flag3 | 0x17 | 1 | u8 | 0 | — | | |
| timestamp | 0x18 | 4 | u32 | 0 | — | | mac↔unix |
| playlist_id | 0x1C | 8 | u64 | 0 | — | | |
| unk0x24 | 0x24 | 4 | u32 | 0 | — | | |
| string_mhod_child_count | 0x28 | 2 | u16 | 0 | — | | |
| playlist_kind_flags | 0x2A | 2 | u16 | 0 | — | | raw kind word; bit0 = podcast, 0x0100 = folder (§7) |
| sort_order | 0x2C | 4 | u32 | 0 | — | | §1.6 |
| parent_folder_playlist_id | 0x30 | 8 | u64 | 0 | — | | parent folder's `playlist_id` |
| unk0x38 | 0x38 | 4 | u32 | 0 | — | | |
| db_id_2 | 0x3C | 8 | u64 | 0 | 0x44 | | |
| playlist_id_2 | 0x44 | 8 | u64 | 0 | 0x4C | | |
| unk0x4C | 0x4C | 4 | u32 | 0 | 0x50 | | |
| mhsd5_type | 0x50 | 2 | u16 | 0 | 0x52 | | built-in rows: Movies=2, TV Shows=3, Music=4, Books=5, Rentals=7 |
| phase_game_flag | 0x52 | 2 | u16 | 0 | 0x54 | | opaque |
| mhsd5_special_flag | 0x54 | 4 | u32 | 0 | 0x58 | | opaque (observed 0x01000000) |
| timestamp_2 | 0x58 | 4 | u32 | 0 | 0x5C | | mac↔unix |

---

## 4. `extraction` — flattening helpers

All functions take already-parsed nested dicts (produced by the parser layer,
another chapter) and return flat dicts. They never mutate their inputs except
where stated (`setdefault` on row dicts in §4.1 — this annotates, never
overwrites, existing keys).

### 4.1 `extract_datasets(mhbd: dict) -> dict`

1. Copy every top-level key of `mhbd` **except** `"children"` into the result
   (all MHBD header fields such as `timezone_offset`, `version`, …).
2. For each `mhsd_wrapper` in `mhbd.get("children", [])`:
   - `mhsd_data = mhsd_wrapper.get("data", {})`;
     `dataset_type = mhsd_data.get("dataset_type")`;
     `result_key = chunk_type_map.get(dataset_type)`; if `None` → skip.
   - **Raw-payload branch first:** if `"raw_payload" in mhsd_data`, set
     `result[result_key] = {"raw_payload_hex": raw.hex(), "genius_cuid": mhsd_data.get("genius_cuid", "")}`
     and continue. (`raw_payload_hex` is lowercase hex, no separators.)
   - If `mhsd_data.get("children", [])` is empty → `result[result_key] = []`.
   - Else take `mhsd_children[0]` as the list chunk; `items = list_chunk.get("data", [])`.
   - For each item: if it is a `dict` containing key `"data"` → unwrap to
     `item["data"]`; otherwise keep as-is. For unwrapped `dict` rows call
     `row.setdefault("_mhsd_dataset_type", dataset_type)` and
     `row.setdefault("_mhsd_result_key", result_key)`.
   - `result[result_key] = flat list of rows`.

Successive datasets with the same key overwrite the previous entry.

### 4.2 `extract_mhod_strings(children: list) -> dict[str, str]`

For each `wrapper` in `children`: `mhod_data = wrapper.get("data", {})`;
`mhod_type = mhod_data.get("mhod_type")` (skip if `None`);
`field_name = mhod_type_map.get(mhod_type)` (skip if falsy/unmapped);
include only when `"string" in mhod_data`; assign
`strings[field_name] = mhod_data["string"]`. **Last occurrence wins.**
Result keys are the readable names from §1.4 (`"Title"`, `"Artist"`, …).

### 4.3 `extract_track_extras(mhod_children: list) -> dict`

Optional key only: `"chapter_data"`. Set when a child has
`mhod_type == MHOD_TYPE_CHAPTER_DATA (17)` and its `"data"` value is a
`dict` (the parser's chapter-tree dict). Last matching child wins.

### 4.4 `extract_playlist_extras(mhod_children: list) -> dict`

Exact key map (each branch additionally requires `"data" in mhod_data`):

| MHOD type | Key | Semantics |
|---|---|---|
| 50 | `"smart_playlist_data"` | SPLPref dict; **last wins** |
| 51 | `"smart_playlist_rules"` | parsed SLst dict; **last wins** |
| 52 | `"library_indices"` | **list**; every occurrence appended in order (created with `setdefault(...).append`) |
| 55 | `PLAYLIST_PROPERTY_KEY` = `"playlist_property_plist"` | the plist dict; also, if `playlist_description_from_row({key: data})` yields a non-empty string, sets `PLAYLIST_DESCRIPTION_KEY` = `"playlist_description"` |
| 100 | `"playlist_prefs"` | column prefs blob; **last wins** |
| 102 | `"playlist_settings"` | settings blob; **last wins** |

Keys absent when no matching child exists.

### 4.5 `extract_playlist_item_extras(mhod_children: list) -> dict`

Runs `extract_mhod_strings` on the MHIP's MHOD children; if a `"Title"`
string exists returns `{"podcast_group_title": <title>}`, otherwise `{}`.
(Used to keep dataset-3 podcast group-header names attached to their MHIP.)

---

## 5. `mhod_defs` — bodies, classification, and smart-playlist tables

### 5.1 Layout constants

| Constant | Value | Meaning |
|---|---|---|
| `MHOD_HEADER_SIZE` | 24 | common header |
| `MHOD_STRING_SUBHEADER_OFFSET` | 0x18 | string sub-header start |
| `MHOD_STRING_SUBHEADER_SIZE` | 16 | encoding + length + unk0x20 + unk0x24 |
| `MHOD_STRING_DATA_OFFSET` | 0x28 | string data start |
| `SPLPREF_BODY_SIZE` | 72 | MHOD 50 body size |
| `SPL_RULE_DATA_SIZE` | 0x44 (68) | non-string rule payload size |
| `MHOD52_BODY_HEADER_SIZE` | 48 | sort_type + count + 40 pad |
| `MHOD53_BODY_HEADER_SIZE` | 16 | sort_type + count + 8 pad |
| `MHOD53_ENTRY_SIZE` | 12 | letter(2) + pad(2) + start(4) + count(4) |
| `MHOD100_POSITION_BODY_SIZE` | 20 | position(4) + pad(16) |
| `SLST_HEADER_SIZE` | 136 | MHOD 51 header |
| `SLST_DEFAULT_UNK004` | 0x00010001 | default/observed version word |
| `SPL_RULE_HEADER_SIZE` | 56 | rule header before payload |
| `SPL_GROUP_MARKER` | 0x01000000 | group-wrapper marker at rule +0x08 |
| `SPL_GROUP_HEADER_BYTES_OFFSET` | 0x0C | opaque tail start |
| `SPL_GROUP_HEADER_BYTES_SIZE` | 40 | opaque tail width |
| `CHAPTER_PREAMBLE_SIZE` | 12 | 3 × u32 LE before chapter atom tree |
| `SEAN_ATOM` / `CHAP_ATOM` / `NAME_ATOM` / `HEDR_ATOM` | `b'sean'` / `b'chap'` / `b'name'` / `b'hedr'` | chapter atom tags |
| `HEDR_SIZE` | 28 | `hedr` atom is always 28 bytes |

### 5.2 Type classification sets

```text
STRING_MHOD_TYPES          = {1..14} ∪ {18..31} ∪ {33..44} ∪ {200..204} ∪ {300}
PODCAST_URL_MHOD_TYPES     = {15, 16}
CHAPTER_DATA_MHOD_TYPES    = {17}
BINARY_BLOB_MHOD_TYPES     = {32}
NON_STRING_MHOD_TYPES      = {50, 51, 52, 53, 55, 100, 102}
```

Anything else is unclassified (unknown-type stub).

### 5.3 `write_mhod_header`

`write_mhod_header(mhod_type: int, total_length: int, unk0x10: int = 0, unk0x14: int = 0) -> bytes`
— packs `'<4sIIIII'`: `b'mhod'`, `24`, `total_length` (complete chunk length =
header + body), `mhod_type`, `unk0x10`, `unk0x14`. Returns 24 bytes.

### 5.4 String MHOD sub-header (offsets relative to MHOD chunk start)

| Off | Size | Meaning |
|---|---|---|
| 0x18 | 4 | encoding: `1` or `0` = UTF-16LE, `2` = UTF-8 |
| 0x1C | 4 | `string_length` — byte count of the string data |
| 0x20 | 4 | opaque (observed `1`) |
| 0x24 | 4 | opaque (observed `0`) |
| 0x28 | `string_length` | string data |

Accessors — all signatures `(data, offset) -> int` with `offset` = MHOD chunk
start: `mhod_string_encoding`, `mhod_string_length`, `mhod_string_unk0x20`,
`mhod_string_unk0x24`. All use `struct.unpack("<I", ...)` on their 4-byte slot.

Format rules (normative, see §0.4): decode exactly `string_length` bytes from
`+0x28`; encoding `2` → UTF-8, encoding `0`/`1` → UTF-16LE; decode errors are
replaced (`errors="replace"`). A conforming writer emits `encoding = 1`,
`string_length = len(value.encode("utf-16-le"))`, and no terminator, with
`total_length = 24 + 16 + string_length`. Podcast URL MHODs (15/16) have
**no** sub-header: UTF-8 body directly after the 24-byte header, length =
`total_length − 24`.

### 5.5 SPLPref — smart playlist preferences (MHOD type 50)

Body offsets are relative to `body_offset = chunk_start + header_length`.
Total body size `SPLPREF_BODY_SIZE = 72` (bytes 14–71 are padding/unknown).

| Off | Size | Accessor | Meaning |
|---|---|---|---|
| 0x00 | 1 | `mhod_spl_live_update` | 1 = auto-update when library changes |
| 0x01 | 1 | `mhod_spl_check_rules` | 1 = apply rules |
| 0x02 | 1 | `mhod_spl_check_limits` | 1 = apply limit |
| 0x03 | 1 | `mhod_spl_limit_type` | see limit-type table |
| 0x04 | 1 | `mhod_spl_limit_sort_raw` | raw sort byte before reverse flag |
| 0x05 | 3 | — | pad |
| 0x08 | 4 | `mhod_spl_limit_value` | u32 LE |
| 0x0C | 1 | `mhod_spl_match_checked_only` | 1 = only matched/checked items |
| 0x0D | 1 | `mhod_spl_reverse_sort` | if set, effective limitsort \|= 0x80000000 |

Limit types: `SPL_LIMIT_TYPE_MINUTES=0x01` → `"minutes"`, `_MB=0x02` → `"MB"`,
`_SONGS=0x03` → `"songs"`, `_HOURS=0x04` → `"hours"`, `_GB=0x05` → `"GB"`
(`SPL_LIMIT_TYPE_MAP`).

Limit sorts (`SPL_LIMIT_SORT_MAP`): `0x02` random, `0x03` song_name, `0x04`
album, `0x05` artist, `0x07` genre, `0x10` most_recently_added,
`0x80000010` least_recently_added, `0x14` most_often_played,
`0x80000014` least_often_played, `0x15` most_recently_played,
`0x80000015` least_recently_played, `0x17` highest_rating,
`0x80000017` lowest_rating. (Integer constants with matching names:
`SPL_LIMIT_SORT_RANDOM`, `_SONG_NAME`, `_ALBUM`, `_ARTIST`, `_GENRE`,
`_MOST_RECENTLY_ADDED`, `_LEAST_RECENTLY_ADDED`, `_MOST_OFTEN_PLAYED`,
`_LEAST_OFTEN_PLAYED`, `_MOST_RECENTLY_PLAYED`, `_LEAST_RECENTLY_PLAYED`,
`_HIGHEST_RATING`, `_LOWEST_RATING`.) The `0x80000000` bit is the reverse flag.

### 5.6 SLst — smart playlist rules (MHOD type 51) — **BIG-ENDIAN**

The `SLst` blob is the only big-endian region of a classic iTunesDB besides
chapter atoms. All multi-byte integers below use `>` formats.

Header (136 bytes), `body_offset` = start of `SLst` data:

| Off | Size | Accessor | Meaning |
|---|---|---|---|
| 0x00 | 4 | `mhod_slst_magic` | must read `b"SLst"` |
| 0x04 | 4 | `mhod_slst_unk004` | BE u32; observed/default `0x00010001` |
| 0x08 | 4 | `mhod_slst_rule_count` | BE u32 rule count |
| 0x0C | 4 | `mhod_slst_conjunction` | BE u32: 0 = AND (all), 1 = OR (any) |
| 0x10 | 120 | — | padding |

Rule layout (`rule_offset` = start of rule; total size = `56 + data_length`):

| Off | Size | Endian | Accessor | Meaning |
|---|---|---|---|---|
| 0x00 | 4 | BE | `mhod_spl_rule_field` | field id (§5.7) |
| 0x04 | 4 | BE | `mhod_spl_rule_action` | action id (§5.8) |
| 0x08 | 4 | BE | `mhod_spl_rule_group_marker` | 0 for leaves, `0x01000000` for group wrappers |
| 0x0C | 40 | raw | `mhod_spl_rule_header_bytes` | opaque tail; leaves have zeros; groups retain bytes verbatim |
| 0x34 | 4 | BE | `mhod_spl_rule_data_length` | payload byte length |
| 0x38 | `data_length` | — | — | payload |

Payloads:

- **Non-string payload** (68 bytes, `data_offset = rule_offset + 0x38`):

  | Off | Size | Format | Accessor |
  |---|---|---|---|
  | 0x00 | 8 | u64 BE | `mhod_spl_rule_from_value` |
  | 0x08 | 8 | **i64** BE | `mhod_spl_rule_from_date` |
  | 0x10 | 8 | u64 BE | `mhod_spl_rule_from_units` |
  | 0x18 | 8 | u64 BE | `mhod_spl_rule_to_value` |
  | 0x20 | 8 | **i64** BE | `mhod_spl_rule_to_date` |
  | 0x28 | 8 | u64 BE | `mhod_spl_rule_to_units` |
  | 0x30 | 4 | u32 BE | `mhod_spl_rule_unk052` |
  | 0x34 | 4 | u32 BE | `mhod_spl_rule_unk056` |
  | 0x38 | 4 | u32 BE | `mhod_spl_rule_unk060` |
  | 0x3C | 4 | u32 BE | `mhod_spl_rule_unk064` |
  | 0x40 | 4 | u32 BE | `mhod_spl_rule_unk068` |

- **String payload** (string-typed fields): `data_length` bytes of **UTF-16BE**
  text; no terminator requirement.
- **Group payload**: a nested SLst-style block (`unk004`, `conjunction`,
  `rules`) reached through the group marker; group wrappers keep
  `field_id`/`action_id`/`data_length`/`group_marker`/`header_bytes` from disk.

Date-rule constants:

- `SPL_DATE_RELATIVE_ACTION_IDS = {0x00000200, 0x02000200}` ("is in the
  last" / "is not in the last").
- `SPL_DATE_IDENTIFIER = 0x2DAE2DAE2DAE2DAE` — must occupy **both**
  `from_value` and `to_value` in relative-date rules; the actual amount and
  unit live in `from_date` (signed) and `from_units`. It is a format sentinel,
  never a timestamp.
- `SPL_DATE_UNITS_MAP`: `1` seconds, `60` minutes, `3600` hours,
  `86400` days, `604800` weeks, `2628000` months (~30.4 days).

### 5.7 `SPL_FIELD_MAP` (field id → label) and `SPL_FIELD_TYPE_MAP`

`SPL_FIELD_MAP`:

| ID | Label | ID | Label | ID | Label |
|---|---|---|---|---|---|
| 0x02 | Song Name | 0x03 | Album | 0x04 | Artist |
| 0x05 | Bit Rate | 0x06 | Sample Rate | 0x07 | Year |
| 0x08 | Genre | 0x09 | Kind | 0x0A | Date Modified |
| 0x0B | Track Number | 0x0C | Size | 0x0D | Time |
| 0x0E | Comment | 0x10 | Date Added | 0x12 | Composer |
| 0x16 | Plays | 0x17 | Last Played | 0x18 | Disc Number |
| 0x19 | Rating | 0x1D | Checked | 0x1F | Compilation |
| 0x23 | BPM | 0x25 | Album Artwork | 0x27 | Grouping |
| 0x28 | Playlist | 0x29 | Purchased | 0x36 | Description |
| 0x37 | Category | 0x39 | Podcast | 0x3C | Media Kind |
| 0x3E | TV Show | 0x3F | Season Number | 0x44 | Skips |
| 0x45 | Last Skipped | 0x47 | Album Artist | 0x4E | Sort Song Name |
| 0x4F | Sort Album | 0x50 | Sort Artist | 0x51 | Sort Album Artist |
| 0x52 | Sort Composer | 0x53 | Sort TV Show | 0x59 | Video Rating |
| 0x5A | Album Rating | 0x85 | Location | 0x86 | Cloud Status |
| 0x9A | Favorite / Suggest Less | 0x9C | Album Favorite / Suggest Less | 0x9F | Work |
| 0xA0 | Movement Name | 0xA1 | Movement Number | | |

Field-type enum: `SPLFT_STRING=1`, `SPLFT_INT=2`, `SPLFT_BOOLEAN=3`,
`SPLFT_DATE=4`, `SPLFT_PLAYLIST=5`, `SPLFT_UNKNOWN=6`, `SPLFT_BINARY_AND=7`.

`spl_get_field_type(field_id: int) -> int` → `SPL_FIELD_TYPE_MAP.get(field_id, SPLFT_UNKNOWN)`.

`SPL_FIELD_TYPE_MAP` (exact memberships):

- STRING: 0x02, 0x03, 0x04, 0x08, 0x09, 0x0E, 0x12, 0x27, 0x36, 0x37, 0x3E,
  0x47, 0x4E, 0x4F, 0x50, 0x51, 0x52, 0x53, 0x59, 0x9F, 0xA0
- INT: 0x05, 0x06, 0x07, 0x0B, 0x0C, 0x0D, 0x16, 0x18, 0x19, 0x23, 0x3F,
  0x44, 0x5A, 0x86, 0x9A, 0x9C, 0xA1, **0x39** (Podcast), **0x3C** (Media Kind)
- DATE: 0x0A, 0x10, 0x17, 0x45
- BOOLEAN: 0x1D, 0x25, 0x1F, 0x29
- PLAYLIST: 0x28
- BINARY_AND: 0x85

### 5.8 `SPL_ACTION_MAP` (action id → label)

Actions are 32-bit bitmapped: bits 24–25 select the class
(`0x00` int/date, `0x01` string, `0x02` negated int/date, `0x03` negated
string); bits 0–10 hold the operator.

| ID | Label | ID | Label |
|---|---|---|---|
| 0x00000001 | is | 0x00000010 | is greater than |
| 0x00000020 | is greater than or equal to | 0x00000040 | is less than |
| 0x00000080 | is less than or equal to | 0x00000100 | is in the range |
| 0x00000200 | is in the last | 0x00000400 | binary AND |
| 0x00000800 | binary unknown1 | 0x01000001 | is (string) |
| 0x01000002 | contains | 0x01000004 | begins with |
| 0x01000008 | ends with | 0x02000001 | is not |
| 0x02000010 | is not greater than | 0x02000020 | is not greater than or equal to |
| 0x02000040 | is not less than | 0x02000080 | is not less than or equal to |
| 0x02000100 | is not in the range | 0x02000200 | is not in the last |
| 0x02000400 | not binary AND | 0x02000800 | binary unknown2 |
| 0x03000001 | is not (string) | 0x03000002 | does not contain |
| 0x03000004 | does not begin with | 0x03000008 | does not end with |

### 5.9 Host-evaluation and authoring sets

Track-dict key maps (the expected keys of a flat track dict used for
host-side membership evaluation):

| Map | Contents (field id → track dict key) |
|---|---|
| `SPL_HOST_STRING_FIELD_KEYS` | 0x02→`"Title"`, 0x03→`"Album"`, 0x04→`"Artist"`, 0x08→`"Genre"`, 0x09→`"filetype"`, 0x0E→`"Comment"`, 0x12→`"Composer"`, 0x27→`"Grouping"`, 0x36→`"Description Text"`, 0x37→`"Category"`, 0x3E→`"Show"`, 0x47→`"Album Artist"`, 0x4E→`"Sort Title"`, 0x4F→`"Sort Album"`, 0x50→`"Sort Artist"`, 0x51→`"Sort Album Artist"`, 0x52→`"Sort Composer"`, 0x53→`"Sort Show"` |
| `SPL_HOST_INT_FIELD_KEYS` | 0x05→`"bitrate"`, 0x06→`"sample_rate_1"`, 0x07→`"year"`, 0x0B→`"track_number"`, 0x0C→`"size"`, 0x0D→`"length"`, 0x16→`"play_count_1"`, 0x18→`"disc_number"`, 0x19→`"rating"`, 0x23→`"bpm"`, 0x39→`"podcast_flag"`, 0x3C→`"media_type"`, 0x3F→`"season_number"`, 0x44→`"skip_count"` |
| `SPL_HOST_DATE_FIELD_KEYS` | 0x0A→`"last_modified"`, 0x10→`"date_added"`, 0x17→`"last_played"`, 0x45→`"last_skipped"` |
| `SPL_HOST_BOOLEAN_FIELD_KEYS` | 0x1D→`"checked_flag"`, 0x1F→`"compilation_flag"`, 0x25→`"has_artwork"`, 0x29→`"purchased_flag"` |
| `SPL_HOST_BINARY_AND_FIELD_KEYS` | 0x85→`"location_kind"` |

Derived sets (exact):

- `SPL_HOST_EVALUABLE_FIELD_IDS = frozenset(union of the five maps' keys | {0x28})`
  — playlist membership (0x28) is supplied separately by the playlist builder.
- `SPL_AUTHORABLE_FIELD_IDS = SPL_HOST_EVALUABLE_FIELD_IDS - {0x39, 0x3E, 0x3F}`
  (Podcast, TV Show, Season Number stay parsed-but-not-authorable).
  Invariant tested: `SPL_AUTHORABLE_FIELD_IDS <= SPL_HOST_EVALUABLE_FIELD_IDS`.
- `SPL_CHOICE_FIELD_IDS = {0x28, 0x3C, 0x85, 0x86, 0x9A, 0x9C}` — menu-valued
  fields.

`SPL_CHOICE_VALUE_MAP: dict[int, tuple[tuple[int, str], ...]]`:

| Field | Choices (raw, label) |
|---|---|
| 0x9A, 0x9C | `(2,"Favorite") (3,"Suggest Less") (0,"None")` |
| 0x86 | `(2,"Matched") (1,"Purchased") (3,"Uploaded") (4,"Ineligible") (5,"Removed") (6,"Error") (7,"Duplicate") (8,"Apple Music") (9,"No Longer Available") (10,"Not Uploaded")` |
| 0x85 | `(1,"on this computer") (2,"iCloud")` |
| 0x3C | `(0x01,"Music") (0x20,"Music Video") (0x02,"Movie") (0x40,"TV Show") (0x04,"Podcast") (0x08,"Audiobook") (0x100000,"Voice Memo") (0x10000,"iTunes Extras")` |

`SPL_CHOICE_UNKNOWN_LABELS = {0x3C: ("Home Video",)}` — label reserved for
media kinds with no proven raw value; never emitted for new writes.

### 5.10 MHOD 52/53 — library playlist index and jump table (LE)

Sort-type constants (shared): `SORT_TITLE=0x03`, `SORT_ALBUM=0x04`,
`SORT_ARTIST=0x05`, `SORT_GENRE=0x07`, `SORT_COMPOSER=0x12`, `SORT_SHOW=0x1D`,
`SORT_SEASON=0x1E`, `SORT_EPISODE=0x1F`, `SORT_ALBUM_ARTIST=0x23`.

`SORT_TYPE_MAP` (id → name): `0x03` title, `0x04` album, `0x05` artist,
`0x07` genre, `0x12` composer, `0x1D` show, `0x1E` season_number,
`0x1F` episode_number, `0x23` album_artist, `0x24` artist_nosort.

- Type 52 body: `+0x00` sort_type u32 LE (`mhod52_sort_type`),
  `+0x04` count u32 LE (`mhod52_count`), `+0x08` 40 bytes pad,
  `+0x30` count × u32 LE sorted track positions.
- Type 53 body: `+0x00` sort_type u32 LE (`mhod53_sort_type`),
  `+0x04` count u32 LE (`mhod53_count`), `+0x08` 8 bytes pad,
  `+0x10` count × 12-byte entries: letter u16 (UTF-16LE unit), pad u16,
  start u32, count u32. The 53 sort_type must match its paired 52.

### 5.11 MHOD 100 — playlist item position (MHIP child)

Body (≤ 20 bytes, `MHOD100_POSITION_BODY_SIZE = 20`):
`+0x00` position u32 LE (`mhod100_position(data, body_offset)`), `+0x04..`
16 bytes pad. The value is the playlist-specific ordering key (strictly
increasing within a playlist in observed databases; equals the MHIP
`group_id` in nearly all playlists).

### 5.12 Chapter data (MHOD 17)

Body starts with `CHAPTER_PREAMBLE_SIZE = 12` bytes (3 × u32 LE unknowns),
then a **big-endian** atom tree using tags `sean`, `chap`, `name`, `hedr`;
the `hedr` atom is always `HEDR_SIZE = 28` bytes. Chapter titles inside
`name` atoms are UTF-16BE. This package only supplies the constants —
parsing/building is the parser/writer chapters' concern.

---

## 6. `playlist_kinds` — kind-bit classification

Constants:

- `PLAYLIST_KIND_PODCAST = 0x0001`
- `PLAYLIST_KIND_FOLDER = 0x0100`

`playlist_kind_flags(value: object) -> int` — always returns a 16-bit word:

- **Mapping input**: `raw = value.get("playlist_kind_flags", value.get("podcast_flag", 0))`;
  `flags = int(raw or 0) & 0xFFFF`, with `TypeError/ValueError/OverflowError` → `0`.
  Then, `is_podcast is True` (identity check on the boolean) → `flags |= 0x0001`;
  `is_folder is True` → `flags |= 0x0100`.
- **Any other input**: `int(value or 0) & 0xFFFF`, failures → `0`.

`is_podcast_playlist(value) -> bool` → `bool(playlist_kind_flags(value) & 0x0001)`.
`is_playlist_folder(value) -> bool` → `bool(playlist_kind_flags(value) & 0x0100)`.

Worked examples (tested behavior):

- `{"is_folder": True}` → `0x0100`
- `{"is_podcast": True}` → `0x0001`
- `{"playlist_kind_flags": 0x0100, "is_folder": False, "is_podcast": True}` → `0x0101`
- a folder row therefore reports `is_folder=True, is_podcast=False` (folders
  do not become podcasts).

---

## 7. `playlist_hierarchy` — folder/parent reconciliation

All public functions are **pure** (they copy rows; inputs are never mutated)
and return `list[dict[str, Any]]` in folder preorder.

### 7.1 Internal helpers (normative behavior)

- `_row_id(row)` → `int(row.get("playlist_id", 0) or 0)`, failures → `0`.
- `_parent_id(row)` → `int(row.get("parent_folder_playlist_id",
  row.get("unk0x30_playlist_ref", 0)) or 0)`, failures → `0`.
  (`unk0x30_playlist_ref` is the legacy alias for the same value.)
- `_item_key(item)` — dedup identity for playlist items:
  1. Non-mapping → `("value", repr(item))`.
  2. Else the **first** of `db_track_id`, `db_id`, `track_persistent_id`,
     `track_id` whose value is not in `(None, "", 0, "0")` → `(field, str(value))`.
  3. Else `source_path` (or `_source_path`) truthy →
     `("source_path", str(path).casefold())`.
  4. Else `("mapping", repr(sorted(item.items(), key=lambda p: str(p[0]))))`.
- `_default_folder_prefs()` →
  `{"live_update": True, "check_rules": True, "check_limits": False,
    "limit_type": 3, "limit_sort": 2, "limit_value": 25,
    "match_checked_only": False}`.

### 7.2 `_normalize_hierarchy_rows(rows)` → `(copied, folder_ids, children, cyclic_folder_ids)`

1. `copied = [dict(row) for row in rows]` (shallow copies, input order kept).
2. `folder_ids` = `{_row_id(row)}` for rows with non-zero id **and**
   `is_playlist_folder(row)` (evaluated against the *original* row before
   flags are rewritten).
3. For each copied row, rewrite in place:
   - `playlist_kind_flags = podcast_flag = playlist_kind_flags(row)` (16-bit),
   - `is_folder = is_playlist_folder(flags)`,
   - `is_podcast = is_podcast_playlist(flags)`,
   - `parent = _parent_id(row)`; if `parent not in folder_ids` **or**
     `parent == _row_id(row)` → `parent = 0`;
     set **both** `parent_folder_playlist_id` and `unk0x30_playlist_ref` to it.
   (Consequence: a parent pointing at a non-folder playlist or a dangling id
   is detached to 0; self-parenting is detached.)
4. Cycle detection over folders only: walk `parent_by_folder_id` from each
   folder; on revisiting a node already on the path, add every id from the
   first occurrence to the end into `cyclic_folder_ids`. For every row whose
   id is cyclic, force `parent_folder_playlist_id = unk0x30_playlist_ref = 0`.
5. `children`: `{folder_id: []}` for all folders; then for each row whose
   (validated) parent is a folder id and differs from its own id, append the
   row to `children[parent]`. Child order = input order.

### 7.3 `_rebuild_folder_contents(folder, child_rows)` (in-place on the copy)

1. **Child union**: iterate `child_rows` in order, then each child's `items`
   in order; keep items whose `_item_key` is new → `child_items`
   (ordered, de-duplicated), and record all keys in `child_item_keys`.
2. **Folder items**: iterate the folder's own `items`; **keep only** items
   whose key is in `child_item_keys` and not yet seen (i.e., stale folder-only
   items are dropped), preserving folder order.
3. Append still-unseen items from `child_items`.
4. Set `folder["items"] = items` and `folder["mhip_child_count"] = len(items)`.
   Mapping items are placed as shallow copies (`dict(item)`), so input item
   dicts are never shared with the result; `items` may be missing or `None`
   on any row (treated as empty).

Worked example (tested): folder items `[103, 999, 101]`, children
`[101, 102]` and `[102, 103]` → result `[103, 101, 102]`,
`mhip_child_count = 3` (999 is not reachable from any child and is dropped).

5. Preferences: `prefs = _default_folder_prefs()`; if the folder already has
   a **Mapping** `smart_playlist_data`, `prefs.update(existing)` (existing
   keys win; missing keys get defaults); assign back.
6. Replace `smart_playlist_rules` with the exact shape:

   ```text
   {"conjunction": "OR",
    "unk004": 0x00010001,
    "rules": [ {"field_id": 0x28, "action_id": 1,
                "from_value": child_id, "from_units": 1,
                "to_value": child_id, "to_units": 1}, ... ]}
   ```

   one rule per direct child row with non-zero id, in child-row order. The
   folder therefore declares membership "playlist is one of my children"
   (SPL playlist-field match, OR).

### 7.4 `_folder_preorder(copied, folder_ids, children)`

Emits each row **once** (identity = `id(row)`):

1. For rows in input order whose parent is **not** a folder: `emit(row)`
   — `emit` appends the row, then recursively `emit`s its children
   (`children.get(row_id, ())`) depth-first.
2. Then for rows in input order: `emit(row)` again (catches rows whose
   parent is a folder but were unreachable, and folder subtrees themselves).

Result: top-level rows (and their whole subtrees) first in input order,
contiguous per folder; children immediately follow their folder.

### 7.5 Public API

`reconcile_playlist_hierarchy(rows: Iterable[Mapping]) -> list[dict]`:

1. Normalize (§7.2).
2. Rebuild folder contents for every folder row (§7.3), recursing
   **children-first**: for a folder, rebuild each child-row that is itself a
   folder before rebuilding this folder (so nested aggregates roll up).
   Each row rebuilt at most once (guarded by `id(row)`).
3. Return `_folder_preorder(...)`.

`refresh_playlist_hierarchy_ancestors(rows, affected_folder_ids) -> list[dict]`
— incremental counterpart:

1. Normalize; `affected = {ids ∩ folder_ids} ∪ cyclic_folder_ids`.
2. Climb `parent_by_id` from each affected id, adding every folder ancestor.
3. `depth(folder_id)` = number of folder ancestors (walk guarded by a `seen`
   set against cycles).
4. Rebuild folder contents for affected ids **sorted by depth descending**
   (deepest descendants first, ancestors last).
5. Return `_folder_preorder(...)` (full ordering is still recomputed).

---

## 8. `playlist_properties` — MHOD-55 plist and description lifecycle

Constants:

- `PLAYLIST_PROPERTY_KEY = "playlist_property_plist"`
- `PLAYLIST_DESCRIPTION_KEY = "playlist_description"`
- `PLAYLIST_DESCRIPTION_DUPLICATE_KEY = "Album"`

Background (format rules): a playlist row can carry its description in two
places — MHOD type 55 (an Apple **binary** plist whose observed key set is at
least `{"description": str}`; preserved whole for unknown future keys) and
MHOD type 3, whose string lands under the global name `"Album"` (type 3 is
"Album" for *tracks*; the duplicate only exists because generic string
extraction names it that way). All plist encode/decode goes through
`plistlib`; `raw_body` inputs may be `bytes`, `bytearray`, or base64 `str`
(decoded with `base64.b64decode`; decode failure → treated as absent).

### 8.1 `PlaylistPropertyPlist` (`@dataclass(slots=True)`)

Fields: `raw_body: bytes | None = None`, `plist: dict[str, Any] = {}`
(factored default per instance).

| Member | Contract |
|---|---|
| `PlaylistPropertyPlist.from_raw_body(raw_body: bytes \| bytearray \| str \| None) -> PlaylistPropertyPlist` | Decode body; `plistlib.loads` failures or non-dict top level → `plist = {}`; returns instance with `raw_body` set when a body was decodable, else an empty instance |
| `.from_parsed(value: object) -> cls` | Instance → returned unchanged. `bytes/bytearray/str/None` → `from_raw_body`. Other non-dict → empty instance. `dict`: `raw_body = decode(value["raw_body"])`; `plist = dict(value["plist"])` if that is a dict else `{}`; if `value["description"]` is a `str`, `plist.setdefault("description", description)`; if the result has no plist **and** a raw body exists → fall back to `from_raw_body(raw_body)` (re-parse from bytes); else return instance with both parts |
| `.from_description(description: str, existing: PlaylistPropertyPlist \| None = None) -> cls` | Copies `existing.plist` (if any), sets `plist["description"] = description`, `raw_body = plistlib.dumps(plist, fmt=plistlib.FMT_BINARY)` |
| `.description` (property) | `plist.get("description")` if `str`, else `""` |
| `.to_parsed_dict() -> dict[str, Any]` | `{"raw_body": self.raw_body or b"", "plist": dict(self.plist)}` plus `"description"` key **only when** `self.description` is non-empty |

### 8.2 Module functions

| Function | Contract |
|---|---|
| `parse_playlist_property_mhod55(raw_body: bytes \| bytearray) -> dict` | `PlaylistPropertyPlist.from_raw_body(raw_body).to_parsed_dict()` — the parser's stable dict shape for MHOD 55 bodies |
| `playlist_property_from_row(row: dict) -> PlaylistPropertyPlist` | `from_parsed(row.get(PLAYLIST_PROPERTY_KEY))` |
| `playlist_description_from_row(row: dict \| None) -> str` | Falsy row → `""`. Else precedence: `row["playlist_description"]` if `str` → that; else property `.description` if non-empty; else `row["Album"]` if `str`; else `""` |
| `normalize_playlist_description(row: dict) -> dict` | **Mutates and returns** `row`. Computes the description; if non-empty **or** `PLAYLIST_DESCRIPTION_KEY` already present, set both `playlist_description` and `Album` to it (empty string included when the key already exists). Otherwise leave the row untouched |
| `playlist_description_update_fields(description: str, existing_row: dict \| None = None) -> dict[str, Any]` | UI-edit result. Returns `{}` when: `description` falsy **and** the existing `raw_body` is missing or empty **and** no existing description. Otherwise returns `{"playlist_description": description, "Album": description, "playlist_property_plist": prop.to_parsed_dict()}` where `prop` is the **existing** property unchanged if (`description == original_description` and `raw_body is not None`), else `PlaylistPropertyPlist.from_description(description, existing_property)` — i.e. unknown plist keys survive a description edit |
| `playlist_property_raw_body_for_write(row: dict) -> bytes \| None` | `description_present` = key exists **and** is a `str`. Start from `playlist_property_from_row(row)`; if `description_present` and row description ≠ property description → rebuild via `from_description(row_desc, prop)`. Then: `prop.raw_body is not None` → return it; else `description_present` → return a freshly built body from the description; else `None` |

Tested round-trip: a plist `{"description": "lalal"}` raw body put in
`row["playlist_property_plist"]["raw_body"]` normalizes to
`playlist_description == Album == "lalal"` and writes back the **identical**
body; editing to `"new"` over `{"description": "old", "future": {...}}`
yields a binary plist `{"description": "new", "future": {...}}`.

---

## 9. `playlist_lifecycle` — edit payload rules

`playlist_edit_payload(existing_row: dict[str, Any] | None, changes: dict[str, Any]) -> dict[str, Any]`
— pure (copies `existing_row`), returns the complete row to save:

1. `row = dict(existing_row or {})`; `row.update(changes)` — the parsed row
   carries format invariants (MHSD membership, result bucket, flags, opaque
   MHOD children); `changes` is only the UI delta.
2. `normalize_playlist_description(row)` (§8.2).
3. Recompute and overwrite, in this order:
   `playlist_kind_flags` = `podcast_flag` = `playlist_kind_flags(row)`;
   `is_folder`; `is_podcast` (§6).
4. Parent: read `parent_folder_playlist_id`, falling back to
   `unk0x30_playlist_ref`; coerce with `int(raw or 0)` (`TypeError`/
   `ValueError`/`OverflowError` → `0`); write the integer to **both**
   `parent_folder_playlist_id` and `unk0x30_playlist_ref`.

Note: unlike hierarchy reconciliation, this function does **not** validate the
parent against folder ids — that happens in `reconcile_playlist_hierarchy`
(§7.2).

---

## 10. `album_identity` — album equality and grouping

- `AlbumIdentity` — `@dataclass(frozen=True)` with `album`, `album_artist`,
  `artist`, `show_name`, all `str | None`.
- `_clean_text(value)` — `None` → `None`; else `str(value).strip()`, and an
  all-whitespace result → `None`.
- `album_identity_from_track(track) -> AlbumIdentity` — reads attributes
  `album`, `album_artist`, `artist`, `show_name` via `getattr(..., None)`,
  each passed through `_clean_text`.
- `album_identity_from_mapping(track: Mapping) -> AlbumIdentity` — first
  truthy key wins per field:
  - album: `"Album"` → `"album"`
  - album_artist: `"Album Artist"` → `"album_artist"`
  - artist: `"Artist"` → `"artist"`
  - show_name: `"Show"` → `"Show Name"` → `"TV Show"` → `"show_name"`
- Comparison helper `_fold(value)` — `None` → `None`; otherwise
  `value.casefold()`. All comparisons below are **case-insensitive**
  (`"KISS"` equals `"Kiss"`), matching how iTunes grouped albums on a real
  iPod Video 5.5G (an exact comparison would split such albums in two).
  The `AlbumIdentity` values themselves keep their original spelling.
- `albums_match(left, right) -> bool` — exact rule order:
  1. `_fold(left.show_name) != _fold(right.show_name)` → `False`
  2. `_fold(left.album) != _fold(right.album)` → `False` (`None` and `""`
     are still **not** equal here)
  3. if **both** `left.album_artist` and `right.album_artist` are truthy →
     return `_fold(left.album_artist) == _fold(right.album_artist)`
  4. otherwise → return `_fold(left.artist) == _fold(right.artist)`
- `AlbumGroup` — `@dataclass` (mutable, generic `Generic[T]`) with
  `identity: AlbumIdentity`, `tracks: list[T]`.
- `group_tracks_by_album_identity(tracks: Iterable[T], identity_fn: Callable[[T], AlbumIdentity]) -> list[AlbumGroup]`:
  - bucket key = `((identity.album or "").casefold(), (identity.show_name or "").casefold())`
  - for each track: compute identity, scan that bucket's group indices in
    insertion order, first group satisfying `albums_match` wins; else append a
    new `AlbumGroup` and register its index in the bucket. A group's
    `identity` is the **first** member's identity, so the album is written
    with the first track's spelling.
  - Groups appear in first-seen order; `tracks` append in input order.
    Bucketing is an optimization only — matches always re-verify with
    `albums_match`.

---

## 11. `device_time` — device clock contexts

### 11.1 Constants and exception

| Name | Value |
|---|---|
| `MAC_EPOCH_OFFSET` | `2_082_844_800` (seconds 1904-01-01 ↔ 1970-01-01) |
| `MAC_EPOCH` | `datetime(1904, 1, 1)` (naive) |
| `MAC_U32_MAX` | `0xFFFF_FFFF` |
| `MacTimestampOutOfRangeError` | subclass of `ValueError`; raised when a UTC instant cannot be represented as a u32 Mac timestamp (device local time ends at 2040-02-06 06:28:15) |

### 11.2 `DeviceTimeContext` (`@dataclass(frozen=True, slots=True)`)

Fields: `timezone: tzinfo`, `name: str`, `source: str`,
`city_id: int | None = None`.

| Classmethod | Contract |
|---|---|
| `DeviceTimeContext.utc()` | `(UTC, "UTC", "utc")` |
| `DeviceTimeContext.fixed_offset(seconds: int, *, source: str = "database_header")` | Raises `ValueError(f"invalid UTC offset: {seconds}")` unless `−86400 ≤ seconds ≤ 86400`; `name = f"UTC{seconds:+d}"` |
| `DeviceTimeContext.from_timezone_name(timezone_name: str, *, source: str = "device_preferences", city_id: int \| None = None)` | Apply `_ZONE_ALIASES` (below), then `ZoneInfo(canonical)`; `ZoneInfoNotFoundError` → `ValueError(f"unknown device timezone {timezone_name!r}")` (original name in message); stores the **canonical** name |

Instance methods:

- `offset_at_unix(unix_timestamp: int) -> int` —
  `datetime.fromtimestamp(int(ts), UTC).astimezone(self.timezone).utcoffset()`
  → whole seconds, `0` when no offset.
- `mac_to_unix(mac_timestamp: int) -> int` — `mac <= 0` → `0`. Else
  `MAC_EPOCH + timedelta(seconds=int(mac))` is a naive *device-local wall
  time*; attach `self.timezone` and return `int(.timestamp())`.
- `unix_to_mac(unix_timestamp: int) -> int` — `int(ts or 0)`; `<= 0` → `0`.
  Convert the UTC instant into `self.timezone`, strip tzinfo (naive local),
  subtract `MAC_EPOCH`. If the result is not `0 < v <= MAC_U32_MAX`, raise
  `MacTimestampOutOfRangeError` with a message naming the local 2040 limit,
  the unix value, and `self.name`. **Never silently clamp.**

Round-trip property (tested): for any representable instant,
`ctx.mac_to_unix(ctx.unix_to_mac(t)) == t`, including across DST.

### 11.3 Active-context plumbing (testability hooks)

- `_active_context: ContextVar[DeviceTimeContext | None]` (default `None`).
- `current_device_time_context() -> DeviceTimeContext` — active value or
  `DeviceTimeContext.utc()` (UTC is the API default; the host timezone never
  leaks in implicitly).
- `active_device_time_context() -> DeviceTimeContext | None` — raw value,
  `None` when unset.
- `use_device_time_context(context)` — `@contextmanager`; sets the var,
  yields, resets via the saved token in `finally` (exception-safe, nests
  correctly).

`field_base.mac_to_unix` / `field_base.unix_to_mac` route every MHIT/MHIP/MHYP
timestamp transform through `current_device_time_context()` — so parsing or
writing inside `use_device_time_context(...)` uses the device zone end-to-end.

### 11.4 City-code timezone table

Used by 2,952/2,956/2,960-byte `Preferences` layouts. The map is private
(`_CITY_TIMEZONE_NAMES`) but its values feed `from_timezone_name`; reproduce
it exactly. Note `0x95` has **no** entry.

| Code | Zone | Code | Zone | Code | Zone |
|---|---|---|---|---|---|
| 0x01 | PST8PDT | 0x02 | America/Chicago | 0x03 | Pacific/Honolulu |
| 0x04 | America/Anchorage | 0x05 | PST8PDT | 0x06 | America/Los_Angeles |
| 0x07 | PST8PDT | 0x08 | PST8PDT | 0x09 | PST8PDT |
| 0x0A | PST8PDT | 0x0B | America/Vancouver | 0x0C | MST7MDT |
| 0x0D | America/Denver | 0x0E | America/Phoenix | 0x0F | MST7MDT |
| 0x10 | CST6CDT | 0x11 | CST6CDT | 0x12 | America/Guatemala |
| 0x13 | America/Managua | 0x14 | CST6CDT | 0x15 | America/Mexico_City |
| 0x16 | CST6CDT | 0x17 | America/Regina | 0x18 | PST8PDT |
| 0x19 | America/El_Salvador | 0x1A | CST6CDT | 0x1B | America/Tegucigalpa |
| 0x1C | America/Winnipeg | 0x1D | EST5EDT | 0x1E | America/Bogota |
| 0x1F | EST5EDT | 0x20 | EST5EDT | 0x21 | America/Detroit |
| 0x22 | America/Havana | 0x23 | America/Indiana/Indianapolis | 0x24 | EST5EDT |
| 0x25 | America/Lima | 0x26 | Europe/London | 0x27 | EST5EDT |
| 0x28 | America/Montreal | 0x29 | America/New_York | 0x2A | EST5EDT |
| 0x2B | America/Panama | 0x2C | EST5EDT | 0x2D | America/Port-au-Prince |
| 0x2E | America/Guayaquil | 0x2F | America/Toronto | 0x30 | EST5EDT |
| 0x31 | America/Asuncion | 0x32 | America/Caracas | 0x33 | America/Guyana |
| 0x34 | America/Halifax | 0x35 | America/La_Paz | 0x36 | America/Argentina/San_Juan |
| 0x37 | America/Santiago | 0x38 | America/Santo_Domingo | 0x39 | America/St_Johns |
| 0x3A | America/Sao_Paulo | 0x3B | America/Argentina/Buenos_Aires | 0x3C | America/Cayenne |
| 0x3D | America/Montevideo | 0x3E | America/Godthab | 0x3F | America/Paramaribo |
| 0x40 | America/Recife | 0x41 | Africa/Casablanca | 0x42 | America/Sao_Paulo |
| 0x43 | Atlantic/South_Georgia | 0x44 | Atlantic/Azores | 0x45 | Europe/Dublin |
| 0x46 | Africa/Accra | 0x47 | Africa/Bamako | 0x48 | Europe/London |
| 0x49 | Africa/Conakry | 0x4A | Africa/Dakar | 0x4B | Europe/Dublin |
| 0x4C | Europe/London | 0x4D | Africa/Freetown | 0x4E | Europe/Lisbon |
| 0x4F | Europe/London | 0x50 | Africa/Monrovia | 0x51 | Africa/Nouakchott |
| 0x52 | Africa/Ouagadougou | 0x53 | Atlantic/Reykjavik | 0x54 | Africa/Algiers |
| 0x55 | Europe/Amsterdam | 0x56 | Africa/Bangui | 0x57 | Europe/Belgrade |
| 0x58 | Europe/Berlin | 0x59 | Europe/Brussels | 0x5A | Europe/Budapest |
| 0x5B | Europe/Copenhagen | 0x5C | Africa/Douala | 0x5D | Europe/Paris |
| 0x5E | Africa/Kinshasa | 0x5F | Africa/Lagos | 0x60 | Europe/Paris |
| 0x61 | Africa/Luanda | 0x62 | Europe/Madrid | 0x63 | Europe/Berlin |
| 0x64 | Africa/Ndjamena | 0x65 | Europe/Oslo | 0x66 | Europe/Paris |
| 0x67 | Europe/Prague | 0x68 | Africa/Casablanca | 0x69 | Europe/Rome |
| 0x6A | Europe/Stockholm | 0x6B | Africa/Tripoli | 0x6C | Africa/Tunis |
| 0x6D | Europe/Vienna | 0x6E | Europe/Warsaw | 0x6F | Europe/Paris |
| 0x70 | Europe/Zurich | 0x71 | Asia/Amman | 0x72 | EET |
| 0x73 | Asia/Beirut | 0x74 | Europe/Bucharest | 0x75 | Africa/Cairo |
| 0x76 | Africa/Johannesburg | 0x77 | Africa/Harare | 0x78 | Europe/Helsinki |
| 0x79 | Europe/Istanbul | 0x7A | Asia/Jerusalem | 0x7B | Africa/Khartoum |
| 0x7C | Europe/Kiev | 0x7D | Africa/Lusaka | 0x7E | Africa/Maputo |
| 0x7F | Europe/Sofia | 0x80 | Africa/Addis_Ababa | 0x81 | Indian/Antananarivo |
| 0x82 | Africa/Asmara | 0x83 | Asia/Baghdad | 0x84 | Asia/Damascus |
| 0x85 | Africa/Dar_es_Salaam | 0x86 | Africa/Djibouti | 0x87 | Asia/Qatar |
| 0x88 | Africa/Kampala | 0x89 | Asia/Bahrain | 0x8A | Asia/Riyadh |
| 0x8B | Africa/Mogadishu | 0x8C | Europe/Moscow | 0x8D | Africa/Nairobi |
| 0x8E | Asia/Riyadh | 0x8F | Asia/Aden | 0x90 | Europe/Moscow |
| 0x91 | Europe/Volgograd | 0x92 | Asia/Dubai | 0x93 | Asia/Muscat |
| 0x94 | Indian/Mauritius | 0x96 | Asia/Karachi | 0x97 | Indian/Maldives |
| 0x98 | Asia/Samarkand | 0x99 | Asia/Yekaterinburg | 0x9A | Asia/Omsk |
| 0x9B | Asia/Dhaka | 0x9C | Asia/Novosibirsk | 0x9D | Asia/Shanghai |
| 0x9E | Asia/Shanghai | 0x9F | Asia/Hong_Kong | 0xA0 | Asia/Kuala_Lumpur |
| 0xA1 | Asia/Manila | 0xA2 | Australia/Perth | 0xA3 | Asia/Shanghai |
| 0xA4 | Asia/Singapore | 0xA5 | Asia/Taipei | 0xA6 | Asia/Shanghai |
| 0xA7 | Asia/Ulaanbaatar | 0xA8 | Australia/Darwin | 0xA9 | Australia/Adelaide |
| 0xAA | Australia/Brisbane | 0xAB | Australia/Melbourne | 0xAC | Pacific/Guam |
| 0xAD | Australia/Hobart | 0xAE | Australia/Melbourne | 0xAF | Australia/Melbourne |
| 0xB0 | Asia/Vladivostok | 0xB1 | Asia/Magadan | 0xB2 | Pacific/Noumea |
| 0xB3 | Asia/Anadyr | 0xB4 | Pacific/Auckland | 0xB5 | America/Adak |
| 0xB6 | Pacific/Pago_Pago | 0xB7 | Asia/Tehran | 0xB8 | Asia/Kabul |
| 0xB9 | Asia/Kolkata | 0xBA | Asia/Colombo | 0xBB | Asia/Kolkata |
| 0xBC | Asia/Kolkata | 0xBD | Asia/Kolkata | 0xBE | Asia/Kathmandu |
| 0xBF | Asia/Tokyo | 0xC0 | Asia/Pyongyang | 0xC1 | Asia/Seoul |
| 0xC2 | Asia/Tokyo | 0xC3 | Asia/Yakutsk | 0xC4 | Europe/Athens |
| 0xC5 | Asia/Rangoon | 0xC6 | Asia/Ho_Chi_Minh | 0xC7 | CST6CDT |
| 0xC8 | Asia/Bangkok | 0xC9 | Asia/Ho_Chi_Minh | 0xCA | Asia/Jakarta |
| 0xCB | Asia/Krasnoyarsk | 0xCC | Asia/Kuwait | 0xCD | Asia/Phnom_Penh |

Zone aliases applied before `ZoneInfo` lookup (`_ZONE_ALIASES`):
`PST8PDT → America/Los_Angeles`, `MST7MDT → America/Denver`,
`CST6CDT → America/Chicago`, `EST5EDT → America/New_York`,
`EET → Europe/Athens`.

### 11.5 `read_device_time_context(ipod_root: str | Path, *, database_offset: int | None = None) -> DeviceTimeContext`

Reads `<ipod_root>/iPod_Control/Device/Preferences` (`OSError` → `b""`), then
takes the **first** matching layout branch:

| Branch condition | Result |
|---|---|
| `len(data) == 2892` (and `>= 0xB12`) | i16 LE at `0xB10`; if `0 <= raw <= 48`: `fixed_offset(((raw − 0x19) >> 1) * 3600 + (3600 if raw & 1 else 0), source="device_preferences")` (whole hours; an odd raw value adds one more hour). If the range check fails, fall through to the bottom fallback |
| `elif len(data) == 2924` (and `>= 0xB24`) | i16 LE at `0xB22` → `fixed_offset(raw * 60 − 8*3600, source="device_preferences")`; out-of-range values propagate `ValueError` from `fixed_offset` |
| `elif len(data) in {2952, 2956, 2960}` (and `>= 0xB72`) | u16 LE city id at `0xB70`; if present in the city table → `from_timezone_name(zone, city_id=city_id)` (`source="device_preferences"`, canonical `name`). Unknown city → fall through |
| bottom fallback | `database_offset is not None` → `fixed_offset(database_offset)` (source `"database_header"`); else `DeviceTimeContext.utc()` |

Precedence: a matching device preference **always** wins over
`database_offset`.

### 11.6 `timezone_changed_since_database(context, database_offset, *, now: int | None = None) -> bool`

- `False` when `database_offset is None` **or**
  `context.source != "device_preferences"` (only preference-derived contexts
  can "change").
- Otherwise compare `context.offset_at_unix(time.time() if now is None else now)`
  with `database_offset`; return whether they differ.

Reference values (for tests): `DeviceTimeContext.from_timezone_name("America/New_York").mac_to_unix(3_868_409_882)` and
`…("Europe/Rome").mac_to_unix(3_868_431_743)` both map to the same-family
instants used in device traces; `unix_to_mac(2_212_122_496)` under UTC must
raise `MacTimestampOutOfRangeError`.

---

## 12. Writer/reader size limits relevant to shared constants

These limits are enforced by the MHOD string writers (other chapter) but
documented here because they bound the layout contracts above:

| Constant | Value |
|---|---|
| `MHOD_STRING_MAX_UTF16_BYTES` | 4096 (standard UTF-16LE strings) |
| `MHOD_LONG_TEXT_MAX_UTF16_BYTES` | 8192 (types 8 Comment, 14 Description, 10 Lyrics) |
| `MHOD_URL_MAX_UTF8_BYTES` | 4096 (types 15/16) |

Truncation is done on encoded bytes with even alignment for UTF-16 and
`errors="ignore"` on the cut, so `string_length` always stays consistent with
`total_length`.

---

## 13. Explicit scope exclusions

The `podsync.itunesdb_shared` package is deliberately dependency-minimal. The
following are **absent by design**:

1. **No GUI / application / podcasts / sqlite / transcoding imports.** None of
   the 18 modules may import from `podsync.gui`, `podsync.application`,
   `podsync.podcasts`, `podsync.sqlitedb_writer`, `podsync.sync`
   (including `sync.transcoder`), or any other sibling package. Docstrings in
   `extraction` mention parser/sync *consumers*; those are other chapters —
   this package never imports them, in either direction.
2. **No SQLite support of any kind.** There is no nano/5G–7G SQLite database
   reader or writer path here. `extract_datasets` only flattens *binary*
   MHBD datasets; a caller expecting a sqlite-backed library representation
   must treat it as unsupported — unknown `dataset_type` values are silently
   skipped (no key, **no exception**), and nothing in this package opens,
   inspects, or emits `.sqlite`/nano database files.
3. **No smart-playlist evaluation engine, editor UI, or formatters.** This
   package provides only the on-disk tables (§5.5–§5.9), classification sets,
   and constants; rule *evaluation*, rule *writing*, and UI behavior live in
   other chapters.
4. **No MHOD body parsing beyond layout accessors.** `mhod_defs` exposes
   offset accessors and size constants; decoding orchestration (dispatch by
   type, string decode loop) belongs to the parser chapter. Conversely, no
   chunk *writers* exist in this package beyond the generic/list builders in
   §2.7 and `write_mhod_header` in §5.3.
5. **No interpretation of unknown/`unk*` fields.** All `unk*`, hash regions,
   and opaque flags (`unk0x22`, `unk0x154`, `phase_game_flag`,
   `mhsd5_special_flag`, group-header tail bytes, etc.) are preserved
   verbatim; assigning them semantics is out of scope. Fields documented as
   "preserve" must round-trip byte-for-byte.
6. **Unsupported MHOD types** (11, 45–49, 54, 56–99, 101, 103–199, 205–299,
   301+) and MHSD types outside 1–10 are not modeled beyond being skipped or
   stubbed; do not add imports or side tables for them in this package.
7. **On-The-Go playlists are absent here.** `itunesdb_shared` defines no
   On-The-Go constants, ids, or helpers; playlist property handling in this
   package is limited to the MHOD-55 plist/description lifecycle of §8. If a
   feature needs On-The-Go semantics, it lives outside this package and this
   chapter.

If an implementation feels it needs any of the above inside
`podsync.itunesdb_shared`, it belongs in another module — raise a clear
`NotImplementedError`/`ValueError` at the boundary instead of importing
excluded packages.

---

## 14. Behaviour checklist

| Area | Package pieces exercised | Must hold |
|---|---|---|
| List chunks | `field_base`: `GENERIC_HEADER_SIZE`, `MHLT_HEADER_SIZE`, `write_list_header`, `write_list_chunk` | header length 92, `"<4sII"` = (tag, 92, count), zero padding after byte 12; chunk body = concatenated children, count = number of children |
| Dataset flattening | `mhsd_defs.MHSD_HEADER_SIZE` (96), `extraction.extract_datasets` | MHSD-9 with raw payload flattens to `{"raw_payload_hex": "<lowercase hex>", "genius_cuid": "<ascii>"}` under key `"mhsd_type_9"`; children absent/empty → list semantics unaffected |
| Device clock | `device_time`: `DeviceTimeContext`, `MacTimestampOutOfRangeError`, `read_device_time_context`, `timezone_changed_since_database`, `use_device_time_context`; `field_base.MAC_EPOCH_OFFSET`; `extraction.extract_datasets` | zone-based `mac_to_unix` correctness; `mac↔unix` round-trip; UTC overflow raises (no clamping); 2956-byte `Preferences` with city `0x69`/`0x29` → `Europe/Rome`/`America/New_York` with `city_id` set; timezone-change detection only for `source == "device_preferences"`; parsed datasets expose header fields (e.g. `timezone_offset`) plus `mhlt` rows |
| Smart-playlist host tables | `mhod_defs`: `SPL_AUTHORABLE_FIELD_IDS`, `SPL_HOST_*_FIELD_KEYS`, `SPL_HOST_EVALUABLE_FIELD_IDS`, `SPL_DATE_IDENTIFIER`; `field_base.MAC_EPOCH_OFFSET`; `device_time.DeviceTimeContext` | `AUTHORABLE ⊆ HOST_EVALUABLE`; union of host key maps covers every track-dict key; marker `0x2DAE2DAE2DAE2DAE` used as sentinel, never decoded as a date; absolute-date rules compare through the device context |
| Smart-playlist field tables | `mhod_defs`: `SPL_FIELD_MAP`, `SPL_FIELD_TYPE_MAP`, `SPLFT_*`, `SPL_DATE_IDENTIFIER`, `SPL_AUTHORABLE_FIELD_IDS`, `SPL_HOST_EVALUABLE_FIELD_IDS`, `MHOD_HEADER_SIZE` | exact labels for 0x1D/0x25/0x59/0x85/0x86/0x9A/0x9C/0x9F/0xA0/0xA1; BOOLEAN = exactly {0x1D, 0x25, 0x1F, 0x29}; 0x85 = BINARY_AND; 0x3C/0x86/0x9A/0x9C/0xA1 = INT; 0x59/0x9F/0xA0 = STRING; non-authorable ids (0x39, 0x3E, 0x3F, 0x59, 0x5A, 0x86, 0x9A, 0x9C, 0x9F, 0xA0, 0xA1) ∉ `SPL_AUTHORABLE_FIELD_IDS` |
| Playlist property plist | `playlist_properties` (all public functions), `extraction.extract_playlist_extras`, `mhod_defs.write_mhod_header`/`MHOD_HEADER_SIZE`, `constants.MHOD_TYPE_PLAYLIST_PROPERTY_PLIST` | MHOD-55 body round-trips byte-identically; `playlist_description` extracted from plist; description edit preserves unknown plist keys; `normalize_playlist_description` sets `playlist_description` **and** `Album`; `playlist_property_raw_body_for_write` returns identical bytes when unchanged |
| Playlist folders | `playlist_kinds.playlist_kind_flags`, `playlist_hierarchy.reconcile_playlist_hierarchy`, `constants.MEDIA_TYPE_PODCAST` | folder bit `0x0100` with `is_podcast is False`; `{"flags":0x0100, "is_folder":False, "is_podcast":True}` → `0x0101`; contiguous folder preorder `[root, folder, child…, loose…]`; folder union drops folder-only items (`[103,999,101] + children → [103,101,102]`, count 3); default folder prefs with `check_rules True`; generated rules exactly `{field_id:0x28, action_id:1, from/to_value: child_id, from/to_units: 1}` under `conjunction "OR"`, `unk004 0x00010001`; dangling parents → 0; parent cycles → both parents 0; nested folders roll up one level per depth; reconciliation is pure (inputs unchanged) |

---

### Implementation notes / known-opaque values

- The `MHYP` fields `phase_game_flag`, `mhsd5_special_flag`, and the many
  `unk*` MHIT fields are format evidence, not logic inputs; round-trip them
  and nothing else.
- `extract_playlist_extras` uses **last-wins** for types 50/51/100/102 but an
  **appending list** for type 52 — this asymmetry is intentional.
- `read_field` treats `header_length=None` as "all guarded fields absent"
  (returns defaults), which matters when a caller parses headers without
  capturing their length.
- `write_fields` distinguishes *absent key* (use default / raise if required)
  from *present key with falsy value* (write the falsy value).
- `strip_article` checks prefixes in the order `'the '`, `'a '`, `'an '` —
  exact strings including the trailing space.
