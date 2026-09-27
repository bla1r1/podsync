# 04 — Writing iTunesDB (`podsync.itunesdb_writer`)

This chapter specifies the complete write path for `iTunesDB` / `iTunesCDB`
files: every module in `podsync.itunesdb_writer`, every byte it emits, the
three checksum algorithms, and the end-to-end install orchestration.
It is written to be reimplementable without access to any other source; all
shared layout tables that the writer depends on are duplicated here verbatim.

Conventions used throughout:

- Offsets are hex, absolute within the chunk unless noted.
- Multi-byte integers in `mhbd`/`mhsd`/`mhlt`/`mhit`/`mhod`/`mhyp`/`mhia`/
  `mhii`/`mhip` bodies are **little-endian**. Smart-playlist (`SLst`) bodies
  and chapter atoms are **big-endian** — called out per section.
- "chunk header" always means the 12-byte generic header (§2.1).
- Error/log message text is normative; the product-name token in those
  messages is `podsync`.

---

## 1. Module map

```
podsync/itunesdb_writer/
├── __init__.py          write_checksum + public re-exports (__all__)
├── mhbd_writer.py       write_mhbd, write_itunesdb, extract_db_info,
│                        extract_preserved_mhsd_blobs + install helpers
├── mhsd_writer.py       MHSD dataset wrappers (types 1,2,3,4,5,8 + stubs)
├── mhlt_writer.py       write_mhlt — track list (MHSD type 1 child)
├── mhit_writer.py       TrackInfo dataclass, generate_db_track_id, write_mhit
├── mhod_writer.py       string MHODs, podcast URLs, chapters, write_track_mhods
├── mhod52_writer.py     library indices (MHOD 52/53), write_library_indices
├── mhod_spl_writer.py   smart playlist MHOD 50/51 + 102/55 passthrough
├── mhlp_writer.py       playlist lists (MHLP) for datasets 2/3/5
├── mhla_writer.py       album list (MHLA/MHIA, MHSD type 4)
├── mhli_writer.py       artist list (MHLI/MHII, MHSD type 8)
├── mhyp_writer.py       playlist rows (MHYP), playlist prefs MHOD 100
├── mhip_writer.py       playlist items (MHIP) + position MHOD 100
├── hash58.py            HMAC-SHA1 signature at mhbd+0x58 (Classic/Nano 3G–4G)
├── hash72.py            AES signature at mhbd+0x72 (Nano 5G) + HashInfo file
└── hashab.py            signature at mhbd+0xAB (Nano 6G/7G) — unsupported,
                         raises (§6.4)
```

The writer imports these shared packages (all part of the reimplementation):

| Package | Used for |
|---|---|
| `podsync.itunesdb_shared.field_base` | `FieldDef`, `read_fields`, `write_fields`, `write_generic_header`, `write_list_header`, `write_list_chunk`, header-size constants |
| `podsync.itunesdb_shared.mhbd_defs` / `mhsd_defs` / `mhit_defs` / `mhod_defs` / `mhyp_defs` / `mhip_defs` / `mhia_defs` / `mhii_defs` | declarative field tables (duplicated in §2) |
| `podsync.itunesdb_shared.constants` | MHOD type IDs, `MEDIA_TYPE_*`, `FILETYPE_CODES`, sort-type IDs |
| `podsync.itunesdb_shared.playlist_kinds` | `playlist_kind_flags` (chapter 03 §6; this chapter calls it "normalising the kind word"), `is_podcast_playlist`, `is_playlist_folder` |
| `podsync.itunesdb_shared.playlist_hierarchy` | `reconcile_playlist_hierarchy` (folder contract) |
| `podsync.itunesdb_shared.album_identity` | `album_identity_from_track`, `group_tracks_by_album_identity` |
| `podsync.itunesdb_shared.device_time` | `read_device_time_context`, `use_device_time_context`, `active_device_time_context` |
| `podsync.device` | `ChecksumType`, `detect_checksum_type`, `get_firewire_id`, `DeviceCapabilities`, storage-safety helpers (`FileSizeLimitError`, `DeviceWriteSafetyError`, durable file ops, path guards) |

Out-of-scope imports (must **not** be required by this package): gui,
application, podcasts, `sync.transcoder`, `sqlitedb_writer`,
`artworkdb_writer` — with one exception: `write_itunesdb` performs a lazy
import of `podsync.artworkdb_writer.artwork_writer`
(`write_artworkdb`, `PendingArtworkWrite`) **only** when
`pc_file_paths is not None` (§7 step 5).

### 1.1 Package `__init__` surface

`podsync.itunesdb_writer.__all__`, in exact order:

`ChecksumType`, `detect_checksum_type`, `get_firewire_id`, `compute_hash58`,
`write_hash58`, `compute_hash72`, `write_hash72`, `read_hash_info`,
`extract_hash_info`, `extract_hash_info_to_dict`, `compute_hashab`,
`write_hashab`, `write_checksum`, `TrackInfo`, `write_mhit`,
`MEDIA_TYPE_AUDIO`, `MEDIA_TYPE_VIDEO`, `MEDIA_TYPE_PODCAST`,
`MEDIA_TYPE_VIDEO_PODCAST`, `MEDIA_TYPE_AUDIOBOOK`, `MEDIA_TYPE_MUSIC_VIDEO`,
`MEDIA_TYPE_TV_SHOW`, `MEDIA_TYPE_RINGTONE`, `write_mhbd`, `write_itunesdb`,
`extract_db_info`, `write_mhli`, `write_mhii_artist`, `write_mhli_empty`,
`PlaylistInfo`, `write_playlist`, `write_mhyp`, `SmartPlaylistPrefs`,
`SmartPlaylistRules`, `SmartPlaylistRule`, `RuleGroup`, `prefs_from_parsed`,
`rules_from_parsed`.

The module-level function defined here is:

```python
def write_checksum(itdb_data: bytearray, ipod_path: str) -> bool
```

| `detect_checksum_type(ipod_path)` result | Action |
|---|---|
| `ChecksumType.NONE` | return `True` (no bytes changed) |
| `ChecksumType.HASH58` | `write_hash58(itdb_data, get_firewire_id(ipod_path))`; `True` |
| `ChecksumType.HASH72` | `write_hash72(itdb_data, ipod_path)`; `True` |
| `ChecksumType.HASHAB` | `write_hashab(...)`, which raises `NotImplementedError` (§6.4) |
| anything else | `raise ValueError(f"Unsupported checksum type: {checksum_type}.")` |

Side effect: mutates `itdb_data` in place; never touches the filesystem
except by reading device files (`get_firewire_id`, HashInfo).

---

## 2. Shared serialization primitives

### 2.1 Generic chunk header (12 bytes)

Packed by `write_generic_header(buffer, offset, tag, header_length,
total_length_or_count)` with `struct "<4sII"`:

| Offset | Size | Type | Meaning |
|---|---|---|---|
| 0x00 | 4 | ASCII | chunk tag (`mhbd`, `mhsd`, `mhlt`, `mhit`, `mhod`, `mhla`, `mhli`, `mhlp`, `mhyp`, `mhip`, `mhia`, `mhii`) |
| 0x04 | 4 | u32 | header length (size of this header region incl. fields) |
| 0x08 | 4 | u32 | **item chunks**: total chunk length; **list chunks** (`mhlt`/`mhla`/`mhli`/`mhlp`): child count |

### 2.2 List headers (`mhlt`, `mhla`, `mhli`, `mhlp`)

`write_list_header(tag, header_length, child_count)` builds
`bytearray(header_length)` with the generic header written into it; the
remainder is zero. All four list headers use `header_length = 92`
(`MHLT_HEADER_SIZE = MHLA_HEADER_SIZE = MLI… = MHLP_HEADER_SIZE = 92`).
`write_list_chunk(tag, header_length, children)` = header + concatenated
children.

### 2.3 Dataset header (`mhsd`) — 96 bytes

| Offset | Size | Type | Field | Notes |
|---|---|---|---|---|
| 0x00 | 12 | — | generic header | `b"mhsd"`, 96, total = 96 + len(child) |
| 0x0C | 4 | u32 | `dataset_type` | required: 1 tracks, 2 playlists, 3 podcast list, 4 albums, 5 smart/builtin categories, 6/10 stubs, 8 artists, 7/9 preserved |
| 0x10..0x60 | 80 | — | zero | |

`write_mhsd(dataset_type, child_data)` is the only builder;
`write_mhsd_type1/2/3/4/smart_type5/…` are one-line wrappers, and
`write_mhsd_empty_stub(dataset_type)` wraps an **empty MHLT** list header
(`write_list_header(b"mhlt", 92, 0)`) — total length 96 + 92 = 188 — which
is how the type 6 and 10 stubs look on disk.

### 2.4 Database header (`mhbd`) — 244 bytes (0xF4)

`MHBD_HEADER_SIZE = 244`. Bytes 0x00–0x0B come from the generic header
(`b"mhbd"`, 244, total file length). The field table below is
`MHBD_FIELDS` verbatim, with the value `write_mhbd` assigns to each field.
`min hdr` = the field is skipped when writing a header shorter than this.

| Offset | Size | Type | Field | min hdr | Value written by `write_mhbd` |
|---|---|---|---|---|---|
| 0x0C | 4 | u32 | `compressed` (default 1) | — | 2 if `capabilities.supports_compressed_db` else 1 |
| 0x10 | 4 | u32 | `version` (required) | — | see §5.2 (`max(ref, cap)` → ref → `0x4F`) |
| 0x14 | 4 | u32 | `child_count` | — | number of MHSD datasets incl. preserved blobs |
| 0x18 | 8 | u64 | `db_id` (required) | — | param → reference → `random.getrandbits(64)` |
| 0x20 | 2 | u16 | `platform` (default 2) | — | 1 = Mac, 2 = Windows (§7 step 4) |
| 0x22 | 2 | u16 | `unk0x22` (default 0) | — | reference value else `611` |
| 0x24 | 8 | u64 | `db_id_2` | — | reference else `random.getrandbits(64)` |
| 0x2C | 4 | u32 | `unk0x2c` | — | `0` |
| 0x30 | 2 | u16 | `hashing_scheme` | — | `0` (writer emits 0; `write_itunesdb` patches — §6) |
| 0x32 | 20 | raw | `unk0x32` | — | reference bytes if exactly 20, else 20 zero bytes |
| 0x46 | 2 | raw | `language` (default `b"en"`) | — | reference language else `language.encode()[:2]` zero-padded to 2 |
| 0x48 | 8 | u64 | `db_persistent_id` | — | reference value else `db_id` |
| 0x50 | 4 | u32 | `unk0x50` (default 1) | — | reference else `1` |
| 0x54 | 4 | u32 | `unk0x54` (default 15) | — | reference else `15` |
| 0x58 | 20 | raw | `hash58` | — | zeros (filled by checksum step) |
| 0x6C | 4 | i32 | `timezone_offset` | — | active device-time context → reference → local `-altzone`/`-timezone` |
| 0x70 | 2 | u16 | `hash_type_indicator` | — | reference value (default 0); else `{HASHAB: 4, HASH72: 2}` from `capabilities.checksum`; else `0` |
| 0x72 | 46 | raw | `hash72` | — | zeros (filled by checksum step) |
| 0xA0 | 2 | u16 | `audio_language` | 0xA2 | copied from reference if present |
| 0xA2 | 2 | u16 | `subtitle_language` | 0xA4 | copied from reference if present |
| 0xA4 | 2 | u16 | `unk0xa4` | 0xA6 | copied from reference if present |
| 0xA6 | 2 | u16 | `unk0xa6` | 0xA8 | copied from reference if present |
| 0xA8 | 2 | u16 | `cdb_flag` | 0xAA | copied from reference if present; later set to 1 for compressed iTunesCDB |
| 0xAB | 57 | raw | `hashab` | 0xE4 | zeros (filled by checksum step) |
| 0xE4..0xF4 | 16 | — | padding | — | zero (header buffer is `bytearray(244)`) |

Named offsets exported by `mhbd_defs` (used by all hash modules):
`MHBD_OFFSET_DB_ID=0x18`, `MHBD_OFFSET_HASHING_SCHEME=0x30`,
`MHBD_OFFSET_UNK_0x32=0x32`, `MHBD_OFFSET_HASH58=0x58`,
`MHBD_OFFSET_HASH72=0x72`, `MHBD_OFFSET_HASHAB=0xAB`.

### 2.5 Field writer (`podsync.itunesdb_shared.field_base`)

- `write_fields(buffer, base_offset, section_type, values, header_length)`
  iterates `FIELD_REGISTRY[section_type]` in offset order; fields whose
  `min_header_length > header_length` are skipped; missing `required` fields
  raise `MissingRequiredFieldError`; missing optional fields use the
  `FieldDef.default`. Every writer builds a plain `dict` of logical values
  and calls this — no manual `struct` packing for table-driven fields.
- `write_field` applies `write_transform`, then `validator` (wrapping
  `ValueError`/`TypeError` into `InvalidFieldValueError`), coerces to the
  struct type and clamps integer values into the format's range before
  `pack_into`.
- Time fields carry `read_transform=mac_to_unix` / `write_transform=unix_to_mac`
  — callers always pass **Unix** timestamps; the writer emits Mac timestamps.
- `read_fields(data, base, section_type, header_length)` (used by
  `extract_db_info` and `write_itunesdb` for reference extraction) returns a
  `dict` of field name → logical value (Mac→Unix already applied).
- Exceptions: `WriteError` > `MissingRequiredFieldError` /
  `InvalidFieldValueError` (defined in `field_base`).

Helpers: `mac_to_unix`/`unix_to_mac` (epoch delta conversions),
`sample_rate_to_fixed`/`fixed_to_sample_rate`, `clamp_rating`,
`validate_rating` (0–100), `validate_volume` (−255..255),
`strip_article` (leading "A/An/The" removal for sort keys).

---

## 3. Dataclasses

### 3.1 `TrackInfo` (`mhit_writer`)

```python
@dataclass
class TrackInfo:
    title: str                              # required
    location: str                           # required; ":iPod_Control:…" path
```

All remaining fields with defaults (declaration order is normative):

| Group | Field | Type | Default | Notes |
|---|---|---|---|---|
| File | `size` | `int` | 0 | bytes |
| File | `length` | `int` | 0 | ms |
| File | `filetype` | `str` | `'mp3'` | key into `FILETYPE_CODES` |
| File | `bitrate` | `int` | 0 | kbps |
| File | `sample_rate` | `int` | 44100 | Hz |
| File | `vbr` | `bool` | False | |
| Metadata | `artist` | `str \| None` | None | |
| Metadata | `album` | `str \| None` | None | |
| Metadata | `album_artist` | `str \| None` | None | |
| Metadata | `genre` | `str \| None` | None | |
| Metadata | `composer` | `str \| None` | None | |
| Metadata | `comment` | `str \| None` | None | |
| Metadata | `year` | `int` | 0 | |
| Metadata | `track_number` | `int` | 0 | |
| Metadata | `total_tracks` | `int` | 0 | |
| Metadata | `disc_number` | `int` | 1 | |
| Metadata | `total_discs` | `int` | 1 | |
| Metadata | `bpm` | `int` | 0 | |
| Metadata | `compilation_flag` | `bool` | False | |
| Playback | `rating` | `int` | 0 | 0–100 |
| Playback | `play_count` | `int` | 0 | |
| Playback | `play_count_2` | `int` | 0 | |
| Playback | `skip_count` | `int` | 0 | |
| Playback | `volume` | `int` | 0 | −255..255 |
| Playback | `start_time` | `int` | 0 | ms |
| Playback | `stop_time` | `int` | 0 | ms |
| Playback | `sound_check` | `int` | 0 | |
| Playback | `bookmark_time` | `int` | 0 | ms |
| Playback | `checked_flag` | `int` | 0 | |
| Gapless | `gapless_data` | `int` | 0 | |
| Gapless | `gapless_track_flag` | `int` | 0 | |
| Gapless | `gapless_album_flag` | `int` | 0 | |
| Gapless | `pregap` | `int` | 0 | |
| Gapless | `postgap` | `int` | 0 | |
| Gapless | `sample_count` | `int` | 0 | u64 |
| Gapless | `encoder_flag` | `int` | 0 | |
| Flags | `skip_when_shuffling` | `bool` | False | |
| Flags | `remember_position` | `bool` | False | |
| Flags | `podcast_flag` | `int` | 0 | → `use_podcast_now_playing_flag` @0xA7 |
| Flags | `movie_file_flag` | `int` | 0 | 0 = auto-derive (§4.3) |
| Flags | `played_mark` | `int` | −1 | −1 = auto-derive |
| Flags | `explicit_flag` | `int` | 0 | |
| Flags | `purchased_aac_flag` | `int` | 0 | |
| Flags | `has_lyrics` | `bool` | False | |
| Flags | `lyrics` | `str \| None` | None | MHOD 10 |
| Flags | `eq_setting` | `str \| None` | None | MHOD 7 |
| Timestamps | `date_added` | `int` | 0 | 0 → `int(time.time())` |
| Timestamps | `date_released` | `int` | 0 | |
| Timestamps | `last_modified` | `int` | 0 | 0 → `date_added` |
| Timestamps | `last_played` | `int` | 0 | |
| Timestamps | `last_skipped` | `int` | 0 | |
| iPod | `track_id` | `int` | 0 | assigned by `write_mhlt` |
| iPod | `db_track_id` | `int` | 0 | 0 → random u64 |
| iPod | `media_type` | `int` | `MEDIA_TYPE_AUDIO` (1) | |
| iPod | `season_number` | `int` | 0 | |
| iPod | `episode_number` | `int` | 0 | |
| iPod | `artwork_count` | `int` | 0 | |
| iPod | `artwork_size` | `int` | 0 | |
| iPod | `mhii_link` | `int` | 0 | artwork image id |
| iPod | `album_id` | `int` | 0 | assigned by `write_mhla` if 0 |
| iPod | `source_path` | `str \| None` | None | **not** written to disk |
| iPod | `source_relative_path` | `str \| None` | None | **not** written to disk |
| Sorting | `sort_artist` | `str \| None` | None | MHOD 23 |
| Sorting | `sort_name` | `str \| None` | None | MHOD 27 |
| Sorting | `sort_album` | `str \| None` | None | MHOD 28 |
| Sorting | `sort_album_artist` | `str \| None` | None | MHOD 29 |
| Sorting | `sort_composer` | `str \| None` | None | MHOD 30 |
| Extra | `grouping` | `str \| None` | None | MHOD 13 |
| Extra | `keywords` | `str \| None` | None | MHOD 24 |
| Podcast | `podcast_enclosure_url` | `str \| None` | None | MHOD 15 |
| Podcast | `podcast_rss_url` | `str \| None` | None | MHOD 16 |
| Podcast | `category` | `str \| None` | None | MHOD 9 |
| Video | `description` | `str \| None` | None | MHOD 14 |
| Video | `subtitle` | `str \| None` | None | MHOD 18 |
| Video | `show_name` | `str \| None` | None | MHOD 19 |
| Video | `episode_id` | `str \| None` | None | MHOD 20 |
| Video | `network_name` | `str \| None` | None | MHOD 21 |
| Video | `sort_show` | `str \| None` | None | MHOD 31 |
| Video | `show_locale` | `str \| None` | None | MHOD 25 |
| Misc | `filetype_desc` | `str \| None` | None | MHOD 6 |
| Round-trip | `user_id` | `int` | 0 | |
| Round-trip | `app_rating` | `int` | 0 | |
| Round-trip | `mpeg_audio_type` | `int` | 0 | |
| Store | `date_added_to_itunes` | `int` | 0 | |
| Store | `store_track_id` | `int` | 0 | |
| Store | `store_encoder_version` | `int` | 0 | |
| Store | `store_artist_id` | `int` | 0 | |
| Store | `store_album_id` | `int` | 0 | |
| Store | `store_content_flag` | `int` | 0 | |
| Internal | `artist_id` | `int` | 0 | assigned by writer |
| Internal | `composer_id` | `int` | 0 | assigned by writer |
| Chapters | `chapter_data` | `dict \| None` | None | `{"chapters": [...]}` → MHOD 17 |
| Transient | `_iop_artwork_sync_hint` | `str` | `""` | never serialized |

Property: `db_id` aliases `db_track_id` (getter and setter).

`generate_db_track_id() -> int` = `random.getrandbits(64)`;
module alias `generate_db_id = generate_db_track_id`.

### 3.2 `PlaylistInfo` and `PlaylistItemMeta` (`mhyp_writer`)

```python
@dataclass
class PlaylistItemMeta:
    podcast_group_flag: int = 0        # MHIP +0x10
    group_id: int = 0                  # MHIP +0x14
    podcast_group_ref: int = 0         # MHIP +0x20
    track_persistent_id: int = 0       # MHIP +0x2C
    mhip_persistent_id: int = 0        # MHIP +0x3C
```

```python
@dataclass
class PlaylistInfo:
    name: str
    track_ids: list[int] = field(default_factory=list)   # db_track_ids!
    playlist_id: int | None = None        # 64-bit; random if None
    master: bool = False                  # MHYP +0x14 type byte = 1
    sortorder: int = 0                    # MHYP +0x2C
    podcast_flag: int = 0                 # legacy spelling of kind word
    playlist_kind_flags: int | None = None  # raw u16 at MHYP +0x2A
    parent_folder_playlist_id: int = 0    # MHYP +0x30
    smart_prefs: SmartPlaylistPrefs | None = None   # both set ⇒ smart
    smart_rules: SmartPlaylistRules | None = None
    mhsd5_type: int = 0                   # MHYP +0x50 (dataset 5 category)
    phase_game_flag: int = 0              # MHYP +0x52 raw u16
    raw_mhod100: bytes | None = None      # opaque MHOD 100 body
    raw_mhod102: bytes | None = None      # opaque MHOD 102 body
    raw_mhod55: bytes | None = None       # opaque MHOD 55 body
    playlist_description: str | None = None   # written as MHOD type 3
    item_metadata: list[PlaylistItemMeta] | None = None  # len == len(track_ids)
```

`__post_init__` normalizes the kind word through
`playlist_kind_flags()` (chapter 03 §6) and assigns the result to **both**
`playlist_kind_flags` and `podcast_flag`. Properties: `kind_flags`
(re-normalized), `is_podcast`, `is_folder`, `is_smart`
(both smart fields non-None).

`track_ids` hold **64-bit `db_track_id`s**; `write_mhbd` remaps them to
32-bit MHIT `track_id`s on non-mutating copies (§5.4).

### 3.3 Smart-playlist dataclasses (`mhod_spl_writer`)

```python
@dataclass
class SmartPlaylistPrefs:            # MHOD 50 body
    live_update: bool = True
    check_rules: bool = True
    check_limits: bool = False
    limit_type: int = 0x03           # 1=min 2=MB 3=songs 4=hours 5=GB
    limit_sort: int = 0x02           # low byte; 0x80000000 = reverse
    limit_value: int = 25
    match_checked_only: bool = False

@dataclass
class SmartPlaylistRule:
    field_id: int = 0x02             # SPL_FIELD_MAP code
    action_id: int = 0x01000002      # SPL_ACTION_MAP code
    string_value: str | None = None  # STRING field rules only
    from_value: int = 0
    from_date: int = 0
    from_units: int = 0
    to_value: int = 0
    to_date: int = 0
    to_units: int = 0
    unk052: int = 0                  # five trailing u32s preserved
    unk056: int = 0
    unk060: int = 0
    unk064: int = 0
    unk068: int = 0

@dataclass
class SmartPlaylistRules:            # MHOD 51 (SLst) body
    conjunction: str = "AND"                                  # or "OR"
    rules: list[SmartPlaylistRule | RuleGroup] = field(default_factory=list)
    unk004: int = SLST_DEFAULT_UNK004                         # 0x00010001

@dataclass
class RuleGroup:                     # recursive SLst node
    group: SmartPlaylistRules = field(default_factory=SmartPlaylistRules)
    field_id: int = 0
    action_id: int = 1
    group_marker: int = SPL_GROUP_MARKER                      # 0x01000000
    header_bytes: bytes | None = None                         # 40 bytes or None
```

Field/action/choice codes live in `mhod_defs`:
`SPL_FIELD_MAP`, `SPL_ACTION_MAP`, `SPL_FIELD_TYPE_MAP`
(`SPLFT_STRING=1`, `SPLFT_INT=2`, `SPLFT_BOOLEAN=3`, `SPLFT_DATE=4`,
`SPLFT_PLAYLIST=5`, `SPLFT_UNKNOWN=6`, `SPLFT_BINARY_AND=7`),
`SPL_DATE_RELATIVE_ACTION_IDS = {0x00000200, 0x02000200}`,
`SPL_DATE_IDENTIFIER = 0x2DAE2DAE2DAE2DAE`,
`SPL_LIMIT_TYPE_*` (1..5), `SPL_LIMIT_SORT_*` (e.g. `0x02` random,
`0x10` most recently added, `0x80000010` least recently added, …).

---

## 4. Chunk writers

### 4.1 `mhsd_writer` / `mhlt_writer`

```python
def write_mhsd(dataset_type: int, child_data: bytes) -> bytes
def write_mhsd_type1(track_list_data) -> bytes        # type 1
def write_mhsd_type2(playlist_list_data) -> bytes     # type 2
def write_mhsd_type3(podcast_list_data) -> bytes      # type 3
def write_mhsd_type4(album_list_data) -> bytes        # type 4
def write_mhsd_smart_type5(smart_playlist_data) -> bytes  # type 5
def write_mhsd_type8(artist_list_data) -> bytes       # type 8
def write_mhsd_empty_stub(dataset_type) -> bytes      # header-only child

def write_mhlt(tracks: list[TrackInfo], start_track_id: int, db_id_2: int,
               capabilities=None, db_version: int = 0) -> tuple[bytes, int]
```

`write_mhlt` assigns `track_id = start_track_id, start+1, …` in list order,
calls `write_mhit(track, track_id, db_id_2, capabilities=…, db_version=…)`
for each track, and returns `(mhlt_header + concatenated MHITs,
next_available_track_id)`. Any exception raised by a track write is
re-raised as `type(exc)(f"{exc} (track #{track_id}: {track.artist!r} – {track.title!r})")`
with `from exc` — the original exception **type** is preserved.

### 4.2 `mhit_writer` — the MHIT record

```python
def write_mhit(track: TrackInfo, track_id: int, db_id_2: int = 0,
               capabilities=None, db_version: int = 0) -> bytes
```

Header size selection (`mhit_header_size_for_version`, from `mhit_defs`):

| `db_version` | header size |
|---|---|
| ≤ 0x12 | 0x9C (156) |
| ≤ 0x19 | 0x148 (328) |
| ≤ 0x2D | 0x1F8 (504) |
| otherwise | 0x270 (624) — `MHIT_HEADER_SIZE` |

When `db_version == 0` the default 0x270 is used. `total_length` =
header size + children; the generic header carries it at +0x08.
`child_count` (+0x0C) is the **actual** number of emitted MHODs.

Pre-write normalization (applied to locals; the noted fields are also
written back onto `track`):

| Rule | Behavior |
|---|---|
| `db_track_id` | `_u64`; 0 → `generate_db_track_id()` (mutates `track`) |
| `date_added` | `_u32`; 0 → `int(time.time())` (mutates `track`) |
| `filetype` | lowercased, leading `.` stripped, empty → `"mp3"`; code from `FILETYPE_CODES` (`mp3 0x4D503320`, `m4a 0x4D344120`, `m4p 0x4D345020`, `m4b 0x4D344220`, `m4v 0x4D345620`, `mp4 0x4D503420`, `wav 0x57415620`, `aif`/`aiff 0x41494646`, `aac 0x41414320`), unknown → mp3 code |
| `title` | stripped; empty → `"Unknown Title"` |
| `location` | **required**: empty → `ValueError("track iPod location is empty")`; must start with `":iPod_Control:"` else `ValueError(f"track iPod location must be an iPod path, got {location!r}")` |
| `sample_rate` | < 8000 → 44100; else `min(rate, 48000)`; written to `sample_rate_1` as 16.16 fixed and `sample_rate_2` as f32 |
| `rating` | clamp 0..100 |
| `volume` | clamp −255..255 |
| `bpm`, counts | `_u16`/`_u32` clamps; negatives → 0 |
| `start_time` | ≥ length → 0 |
| `stop_time` | > length → length; then, if non-zero and ≤ start (or start was invalid) → 0 |
| `bookmark_time` | > length → length |
| (the three trim rules) | apply only when `length > 0`; with `length == 0` the three values are written as clamped u32 unchanged |
| `encoder` (+0xCC) | `encoder_flag` |
| `last_modified` | written as `last_modified or date_added`; `track.last_modified` itself is not changed |
| `media_type` | downgraded when capabilities say so: no video → `VIDEO`/`MUSIC_VIDEO`/`TV_SHOW`→`AUDIO`, `VIDEO_PODCAST`→`PODCAST`; no podcast → `PODCAST`/`VIDEO_PODCAST`→`AUDIO` |
| `movie_flag` (+0xB1) | explicit `movie_file_flag` else 1 when media type ∈ {VIDEO, MUSIC_VIDEO, TV_SHOW, VIDEO_PODCAST} else 0 |
| `not_played_flag` (+0xB2) | `played_mark` if ≥ 0 else `0x01` when `play_count > 0` else `0x02` |
| `has_artwork` (+0xA4) | `1` if `artwork_count > 0` else `2` |
| `use_podcast_now_playing_flag` (+0xA7) | raw `podcast_flag` byte |
| `lyrics_flag` (+0xB0) | 1 when `has_lyrics` or `lyrics` non-empty |
| `visible` (+0x14) | always 1 |
| `mp3_flag` (+0x1D) | 1 iff normalized filetype == `mp3` |
| `audio_format_flag` (+0x7E) | `AUDIO_FORMAT_FLAG_MAP = {'wav':0, 'aif':0, 'aiff':0, 'm4b':1}`, else `0xFFFF` |
| `sample_rate`/gapless | `pregap`, `postgap`, `sample_count`, `gapless_audio_payload_size`, `gapless_track_flag`, `gapless_album_flag` written as-is **unless** `capabilities` is given and `supports_gapless` is false → all become 0 |
| `db_track_id_2` (+0xA8) | copy of `db_track_id` |
| `size_2` (+0x12C) | copy of `size` |
| `db_id_2_ref` (+0x124) | the `db_id_2` argument |
| `sort_mhod_indicators` (+0x134, 8 B) | byte *i* = `0x81` if the corresponding sort field is set else `0x80`, for order: `sort_name`(27), `sort_album`(28), `sort_artist`(23), `sort_album_artist`(29), `sort_composer`(30), `sort_show`(31); bytes 6–7 = 0 |
| `artwork_id_ref` (+0x160) | `mhii_link` |
| `artist_id_ref` (+0x1E0) / `composer_id` (+0x1F4) | writer-assigned IDs (§5.3) |
| `album_id` (+0x120) | writer-assigned ID (§5.3) |

Fields not listed keep their `FieldDef` default (e.g. `unk0x84`=0,
`audio_format_flag` fallback, `media_type` default 1).

Child MHODs are built first (`write_track_mhods`) so `child_count` and
`total_length` are known before the header is packed.

#### 4.2.1 Full `MHIT_FIELDS` table (verbatim)

Type codes: `u8`/`u16`/`u32`/`u64`/`i32`/`f32`/`raw N`.
"min" = `min_header_length`; fields below a device's header size are omitted
(zero bytes remain).

| Offset | Type | Field | min | Notes / default |
|---|---|---|---|---|
| 0x0C | u32 | `child_count` | | MHOD count |
| 0x10 | u32 | `track_id` | | required |
| 0x14 | u32 | `visible` | | default 1 |
| 0x18 | u32 | `filetype` | | fourcc code |
| 0x1C | u8 | `vbr_flag` | | |
| 0x1D | u8 | `mp3_flag` | | |
| 0x1E | u8 | `compilation_flag` | | |
| 0x1F | u8 | `rating` | | `clamp_rating`, `validate_rating` |
| 0x20 | u32 | `last_modified` | | Mac↔Unix transform |
| 0x24 | u32 | `size` | | |
| 0x28 | u32 | `length` | | |
| 0x2C | u32 | `track_number` | | |
| 0x30 | u32 | `total_tracks` | | |
| 0x34 | u32 | `year` | | |
| 0x38 | u32 | `bitrate` | | |
| 0x3C | u32 | `sample_rate_1` | | fixed-point transform |
| 0x40 | i32 | `volume` | | `validate_volume` |
| 0x44 | u32 | `start_time` | | |
| 0x48 | u32 | `stop_time` | | |
| 0x4C | u32 | `sound_check` | | |
| 0x50 | u32 | `play_count_1` | | |
| 0x54 | u32 | `play_count_2` | | |
| 0x58 | u32 | `last_played` | | Mac↔Unix |
| 0x5C | u32 | `disc_number` | | |
| 0x60 | u32 | `total_discs` | | |
| 0x64 | u32 | `user_id` | | |
| 0x68 | u32 | `date_added` | | Mac↔Unix |
| 0x6C | u32 | `bookmark_time` | | |
| 0x70 | u64 | `db_track_id` | | required |
| 0x78 | u8 | `checked_flag` | | |
| 0x79 | u8 | `app_rating` | | |
| 0x7A | u16 | `bpm` | | |
| 0x7C | u16 | `artwork_count` | | |
| 0x7E | u16 | `audio_format_flag` | | default 0xFFFF |
| 0x80 | u32 | `artwork_size` | | |
| 0x84 | u32 | `unk0x84` | | |
| 0x88 | f32 | `sample_rate_2` | | |
| 0x8C | u32 | `date_released` | | Mac↔Unix |
| 0x90 | u16 | `mpeg_audio_type` | | |
| 0x92 | u8 | `explicit_flag` | | |
| 0x93 | u8 | `purchased_aac_flag` | | |
| 0x94 | u32 | `unk0x94` | | |
| 0x98 | u32 | `genius_category_id` | | |
| 0x9C | u32 | `skip_count` | 0xA0 | |
| 0xA0 | u32 | `last_skipped` | 0xA4 | Mac↔Unix |
| 0xA4 | u8 | `has_artwork` | 0xA5 | |
| 0xA5 | u8 | `skip_when_shuffling` | 0xA6 | |
| 0xA6 | u8 | `remember_position` | 0xA7 | |
| 0xA7 | u8 | `use_podcast_now_playing_flag` | 0xA8 | |
| 0xA8 | u64 | `db_track_id_2` | 0xB0 | |
| 0xB0 | u8 | `lyrics_flag` | 0xB1 | |
| 0xB1 | u8 | `movie_flag` | 0xB2 | |
| 0xB2 | u8 | `not_played_flag` | 0xB3 | |
| 0xB3 | u8 | `unk0xB3` | 0xB4 | |
| 0xB4 | u32 | `unk0xB4` | 0xB8 | |
| 0xB8 | u32 | `pregap` | 0xBC | |
| 0xBC | u64 | `sample_count` | 0xC4 | |
| 0xC4 | u32 | `unk0xC4` | 0xC8 | |
| 0xC8 | u32 | `postgap` | 0xCC | |
| 0xCC | u32 | `encoder` | 0xD0 | |
| 0xD0 | u32 | `media_type` | 0xD4 | default 1 |
| 0xD4 | u32 | `season_number` | 0xD8 | |
| 0xD8 | u32 | `episode_number` | 0xDC | |
| 0xDC | u32 | `date_added_to_itunes` | 0xE0 | Mac↔Unix |
| 0xE0 | u32 | `store_track_id` | 0xE4 | |
| 0xE4 | u32 | `store_encoder_version` | 0xE8 | |
| 0xE8 | u32 | `store_artist_id` | 0xEC | |
| 0xEC | u32 | `unk0xEC` | 0xF0 | |
| 0xF0 | u32 | `store_album_id` | 0xF4 | |
| 0xF4 | u32 | `store_content_flag` | 0xF8 | |
| 0xF8 | u32 | `gapless_audio_payload_size` | 0xFC | |
| 0xFC | u32 | `unk0xFC` | 0x100 | |
| 0x100 | u16 | `gapless_track_flag` | 0x102 | |
| 0x102 | u16 | `gapless_album_flag` | 0x104 | |
| 0x104 | raw 20 | `hash_0x104` | 0x118 | |
| 0x118 | u32 | `unk0x118` | 0x11C | |
| 0x11C | u32 | `unk0x11C` | 0x120 | |
| 0x120 | u32 | `album_id` | 0x124 | |
| 0x124 | u64 | `db_id_2_ref` | 0x12C | |
| 0x12C | u32 | `size_2` | 0x130 | |
| 0x130 | u32 | `unk0x130` | 0x134 | |
| 0x134 | raw 8 | `sort_mhod_indicators` | 0x13C | |
| 0x154 | u32 | `unk0x154` | 0x158 | default 0 |
| 0x160 | u32 | `artwork_id_ref` | 0x164 | |
| 0x164 | u32 | `unk0x164` | 0x168 | default 0 |
| 0x168 | u32 | `unk0x168` | 0x16C | default 1 |
| 0x173 | u8 | `unk0x173` | 0x174 | default 0 |
| 0x194 | u8 | `movie_flag_2` | 0x195 | default 0 |
| 0x195 | u8 | `purchased_aac_flag_2` | 0x196 | default 0 |
| 0x197 | u8 | `unk0x197` | 0x198 | default 0 |
| 0x1A0 | u32 | `unk0x1A0` | 0x1A4 | default 0 |
| 0x1B0 | u64 | `store_track_id_2` | 0x1B8 | default 0 |
| 0x1B8 | u64 | `store_encoder_version_2` | 0x1C0 | default 0 |
| 0x1C0 | u64 | `store_artist_id_2` | 0x1C8 | default 0 |
| 0x1C8 | u64 | `unk0xEC_2` | 0x1D0 | default 0 |
| 0x1D0 | u64 | `store_album_id_2` | 0x1D8 | default 0 |
| 0x1D8 | u64 | `store_content_flag_2` | 0x1E0 | default 0 |
| 0x1E0 | u32 | `artist_id_ref` | 0x1E4 | |
| 0x1EC | u32 | `unk0x1EC` | 0x1F0 | default 0 |
| 0x1F4 | u32 | `composer_id` | 0x1F8 | |
| 0x1F8 | u32 | `unk0x1F8` | 0x1FC | default 0 |
| 0x20C | u32 | `unk0x20C` | 0x210 | default 0 |
| 0x229 | u8 | `unk0x229` | 0x22A | default 0 |
| 0x22B | u8 | `unk0x22B` | 0x22C | default 0 |

Zero gaps that must remain zero: 0x13C..0x153, 0x158..0x15F, 0x16C..0x172,
0x174..0x193, 0x196, 0x198..0x19F, 0x1A4..0x1AF, 0x1E4..0x1EB,
0x1F0..0x1F3, 0x1FC..0x20B, 0x210..0x228, 0x22C..end-of-header.

### 4.3 `mhod_writer` — strings, URLs, chapters

MHOD common header (24 B), built by `write_mhod_header(mhod_type,
total_length, unk0x10=0, unk0x14=0)` with `struct "<4sIIIII"`:
`b"mhod"`, `MHOD_HEADER_SIZE`=24, total length, type, 0, 0.

**String MHODs** (`STRING_MHOD_TYPES` = 1..14, 18..31, 33..44, 200..204,
300):

```python
def write_mhod_string(mhod_type: int, value: Any,
                      unk_0x20: int = 1, unk_0x24: int = 0) -> bytes
```

| Region | Content |
|---|---|
| 0x00..0x18 | MHOD common header (total = 24 + 16 + len(data)) |
| 0x18 | u32 encoding = 1 (UTF-16LE) |
| 0x1C | u32 string byte length (no terminator) |
| 0x20 | u32 `unk_0x20` (default 1) |
| 0x24 | u32 `unk_0x24` (default 0) |
| 0x28 | UTF-16LE payload, `errors='replace'` |

- Value coercion: `None` → `""`; `bytes`/`bytearray` → UTF-8 decode with
  `errors="replace"`; anything else → `str(value)`. No stripping — a
  whitespace-only string is written as-is.
- Empty input (or a string that truncation reduced to nothing) → returns
  `b''`; callers skip the chunk entirely (MHOD omitted, not zero-length).
- Truncation limits on the **encoded** payload:
  `MHOD_STRING_MAX_UTF16_BYTES = 4096`,
  `MHOD_LONG_TEXT_MAX_UTF16_BYTES = 8192` for types 8 (comment),
  14 (description), 10 (lyrics); truncation cuts on an even byte boundary
  and logs a debug message.
- Convenience wrappers (all delegate to `write_mhod_string` with the fixed
  type from §3.1): `write_mhod_title` (1), `write_mhod_location` (2),
  `write_mhod_album` (3), `write_mhod_artist` (4), `write_mhod_genre` (5),
  `write_mhod_filetype` (6), `write_mhod_comment` (8),
  `write_mhod_composer` (12), `write_mhod_album_artist` (22),
  `write_mhod_sort_artist` (23), `write_mhod_sort_name` (27),
  `write_mhod_sort_album` (28).

**Podcast URLs** (types 15/16) — different layout:

```python
def write_mhod_podcast_url(mhod_type: int, url: Any) -> bytes
```

- Type must be 15 or 16, else `ValueError(f"write_mhod_podcast_url only supports types 15 and 16, got {mhod_type}")`.
- UTF-8 encoded, **no sub-header**: `total_length = 24 + len(utf8)`, payload
  directly at 0x18.
- Truncated to `MHOD_URL_MAX_UTF8_BYTES = 4096` UTF-8 bytes; empty → `b''`.

**Chapter data** (type 17):

```python
def write_mhod_chapter_data(chapters: list[dict], unk024: int = 0,
                            unk028: int = 0, unk032: int = 0) -> bytes
def build_chapter_blob(chapters, unk024=0, unk028=0, unk032=0) -> bytes
```

Body layout (preamble little-endian, atom tree **big-endian**):

| Region | Content |
|---|---|
| 0x18 | 3 × u32 LE: `unk024`, `unk028`, `unk032` (12 bytes) |
| then | `sean` atom: size(4,BE) + `"sean"` + u32 1 + u32 child_count + u32 0 |
| children | one `chap` atom per chapter: size(4) + `"chap"` + startpos u32 + u32 1 + u32 0 + `name` atom |
| `name` atom | size(4) + `"name"` + u32 1 + u32 0 + u32 0 + u16 title_units + UTF-16BE title (size = 22 + 2·units) |
| terminator | `hedr` atom, always 28 bytes: size=28 + `"hedr"` + u32 1 + 4×u32 0 + u32 1 |

`sean` child count = valid chapters + 1 (the `hedr`). `build_chapter_blob`
returns the same bytes minus the 24-byte MHOD header (used by other
subsystems for raw chapter blobs).

Validation via `_normalized_chapters_for_track` (used by
`write_track_mhods` when `track.chapter_data["chapters"]` is present):
non-list, empty, or > `_MAX_CHAPTER_COUNT = 500` → no MHOD; any non-dict
entry, a `startpos` that is not an int in `[0, 0xFFFFFFFF)` or not strictly
greater than the previous one, or a "suspicious" title (contains NUL, more
than `max(1, len(title) // 10)` U+FFFD characters, or any control character
other than tab/CR/LF) invalidates the **whole** list → no MHOD (debug log
`Skipping implausible chapter data MHOD`). Titles are stripped; a missing or
empty title becomes `"Chapter {n}"` (1-based). Other keys of each chapter
dict are kept.

`write_mhod_chapter_data` itself (when called directly) skips non-dict
entries, clamps `startpos` to u32 (bad values → 0), truncates a title to
65 535 UTF-16 units, and returns `b''` when no dict entry remains. The
preamble values `unk024/unk028/unk032` come from `chapter_data` (default 0)
and are clamped to u32.

**`write_track_mhods`** — full signature:

```python
def write_track_mhods(title, location, artist=None, album=None, genre=None,
    album_artist=None, composer=None, comment=None, filetype_desc=None,
    sort_artist=None, sort_name=None, sort_album=None,
    sort_album_artist=None, sort_composer=None, grouping=None,
    description=None, podcast_enclosure_url=None, podcast_rss_url=None,
    subtitle=None, show_name=None, episode_id=None, network_name=None,
    keywords=None, sort_show=None, category=None, lyrics=None,
    eq_setting=None, show_locale=None, chapter_data=None,
    ) -> tuple[bytes, int]
```

Emission order is fixed (each entry only emitted when its value is
truthy):

| # | Value | MHOD type |
|---|---|---|
| 1 | title | 1 |
| 2 | location | 2 |
| 3 | artist | 4 |
| 4 | album | 3 |
| 5 | genre | 5 |
| 6 | album_artist | 22 |
| 7 | composer | 12 |
| 8 | comment | 8 |
| 9 | filetype_desc | 6 |
| 10 | category | 9 |
| 11 | description | 14 |
| 12 | subtitle | 18 |
| 13 | show_name | 19 |
| 14 | episode_id | 20 |
| 15 | network_name | 21 |
| 16 | keywords | 24 |
| 17 | sort_artist | 23 |
| 18 | sort_name | 27 |
| 19 | sort_album | 28 |
| 20 | sort_album_artist | 29 |
| 21 | sort_composer | 30 |
| 22 | sort_show | 31 |
| 23 | show_locale | 25 |
| 24 | grouping | 13 |
| 25 | podcast_enclosure_url | 15 (UTF-8) |
| 26 | podcast_rss_url | 16 (UTF-8) |
| 27 | eq_setting | 7 |
| 28 | lyrics | 10 |
| 29 | chapter_data | 17 |

Returns `(concatenated bytes, count)`; empty strings never add a chunk.

### 4.4 `mhod52_writer` — library indices (MHOD 52/53)

```python
def write_mhod_type52(tracks, sort_type) -> tuple[bytes, list[tuple[int,int,int]]]
def write_mhod_type53(sort_type, jump_entries) -> bytes
def write_library_indices(tracks, capabilities=None) -> tuple[bytes, int]
```

MHOD 52 body (all LE): total = `4·n + 48 + 24` = `4n + 72`;
`sort_type` u32 @ body+0, `count` u32 @ body+4, 40 bytes zero, then
`count` × u32 — each the **original index** of the track in the sorted
order (i.e. permutation mapping sorted position → input position).

Sort key rules (`_get_sort_fields`; always prefer `sort_*` over display
field; strip leading articles + NFKD + casefold):

| sort_type | key tuple |
|---|---|
| `SORT_TITLE` 0x03 | (title,) |
| `SORT_ALBUM` 0x04 | (album, disc, track_nr, title) |
| `SORT_ARTIST` 0x05 | (artist, album, disc, track_nr, title) |
| `SORT_GENRE` 0x07 | (genre, artist, album, disc, track_nr, title) |
| `SORT_COMPOSER` 0x12 | (composer, album, disc, track_nr, title) |
| `SORT_SHOW` 0x1D | (show, season, episode, title) |
| `SORT_SEASON` 0x1E | (season, episode, show, title) |
| `SORT_EPISODE` 0x1F | (episode, season, show, title) |
| `SORT_ALBUM_ARTIST` 0x23 | (album_artist, album, disc, track_nr, title) |

Field sources: `title = sort_name or title`, `album = sort_album or album`,
`artist = sort_artist or artist`, `composer = sort_composer or composer`,
`show = sort_show or show_name`, **`genre` = `genre` only** (there is no
sort-genre), `album_artist = sort_album_artist or album_artist or
sort_artist or artist`. String keys go through `_sort_key`: `None` → `""`,
bytes decoded UTF-8 (`replace`), `strip_article`, then NFKD + casefold.
Numbers (`disc_number`, `track_number`, `season_number`, `episode_number`)
use `value or 0`. An unknown `sort_type` sorts like Title.

Sorting is stable (`list.sort` on the tuple, original order breaks ties).
Jump-table letters come from the **first** field of the category (raw
display/sort text as above, not the casefolded key): first alphanumeric
char, uppercased codepoint; digits → `ord('0')`; empty/no alnum →
`ord('0')`; a non-BMP char is skipped and the scan continues. For Season
and Episode the letter is always `ord('0')` (numbers). Entries are runs of
equal letters in sorted order → `(letter, start, count)`.

MHOD 53 body: total = `12·m + 16 + 24` = `12m + 40`;
`sort_type` u32, `count` u32, 8 bytes zero, then `m` ×
`letter u16, pad u16 = 0, start u32, count u32`.

`write_library_indices` emits 52/53 **pairs**, in this category order:
`BASE_SORT_TYPES` = [Title, Album, Artist, Genre, Composer] always; plus
[Show, Season, Episode] when `capabilities.supports_video`; plus
Album-Artist whenever `capabilities is not None`. Empty `tracks` →
`(b'', 0)`. Returns `(concatenated bytes, mhod_count)`.

### 4.5 `mhod_spl_writer` — smart playlists (MHOD 50/51) and passthroughs

```python
def write_mhod50(prefs: SmartPlaylistPrefs) -> bytes       # total 96
def write_mhod51(rules_data: SmartPlaylistRules) -> bytes  # 24 + SLst
def write_mhod102(raw_body: bytes) -> bytes                # 24 + body
def write_mhod55(raw_body: bytes) -> bytes                 # 24 + body
def prefs_from_parsed(parsed: dict) -> SmartPlaylistPrefs
def rules_from_parsed(parsed: dict) -> SmartPlaylistRules
```

**MHOD 50 (SPLPref)** — header 24 + body 72 = **96 bytes total**
(`total_length` at +8 must equal 96). Body offsets:

| Body off | Content |
|---|---|
| +0x00 | `live_update` 0/1 |
| +0x01 | `check_rules` 0/1 |
| +0x02 | `check_limits` 0/1 |
| +0x03 | `limit_type` low byte |
| +0x04 | `limit_sort` low byte |
| +0x05..+0x07 | zero |
| +0x08 | `limit_value` u32 LE |
| +0x0C | `match_checked_only` 0/1 |
| +0x0D | reverse flag = 1 iff `limit_sort & 0x80000000` |
| +0x0E..+0x47 | zero |

**MHOD 51 (SLst)** — the entire SLst body is **big-endian**:

| Region | Content |
|---|---|
| SLst header (136 B) | `b"SLst"`; +0x04 `unk004` BE (default `0x00010001`); +0x08 rule count BE; +0x0C conjunction BE (`0` = AND, `1` = OR — anything whose uppercase isn't `"OR"` is AND); +0x10..0x88 zero |
| rule header (56 B) | +0x00 `field_id` BE, +0x04 `action_id` BE, +0x08 marker BE (`0` for leaves, `0x01000000` for groups), +0x0C..+0x33 zero (leaves) / 40 opaque bytes (groups), +0x34 data length BE |
| leaf data (STRING) | UTF-16BE payload, ≤ 4096 bytes (cut on even boundary) |
| leaf data (non-string, 68 B) | `>Q from_value, >q from_date, >Q from_units, >Q to_value, >q to_date, >Q to_units` then five BE u32 `unk052, unk056, unk060, unk064, unk068` |
| group data | a complete nested SLst (header + its rules), length written at +0x34 |

Details:
- Leaf vs group: `isinstance(rule, RuleGroup)`.
- `RuleGroup.header_bytes` must be `None` (→ 40 zero bytes) or exactly
  40 bytes; otherwise
  `ValueError("RuleGroup.header_bytes must contain exactly 40 bytes")`.
- A leaf is written as a STRING rule only when its field type is
  `SPLFT_STRING` **and** `string_value is not None`; every other leaf
  (including a string field without a value) gets the 68-byte numeric data.
- Relative-date rules (`spl_get_field_type(field_id) == SPLFT_DATE` and
  `action_id in SPL_DATE_RELATIVE_ACTION_IDS = {0x00000200, 0x02000200}`):
  `from_value` becomes `SPL_DATE_IDENTIFIER = 0x2DAE2DAE2DAE2DAE`;
  `from_date`: if non-zero → `-abs(from_date)` (after i64 clamping); else if
  `from_value` (read as signed i64: values ≥ 2⁶³ wrap negative) is non-zero,
  with `amount = abs(from_value)` and `units = from_units` →
  `-(amount // units)` when `units > 1 and amount >= units and amount % units == 0`,
  otherwise `-amount`; else stays 0. The writer then also forces
  `to_value = SPL_DATE_IDENTIFIER`, `to_date = 0`, `to_units = 1`.
- Integer coercion is **clamping**, not masking: `int(value or 0)` (errors →
  0), then unsigned u32/u64 fields clamp to `[0, max]` (a negative becomes 0)
  and the signed `from_date`/`to_date` clamp to the i64 range. `limit_type`
  and the `limit_sort` low byte are taken after u32 clamping.
- UTF-16BE string payloads longer than 4096 bytes are cut to 4096.

`prefs_from_parsed` accepts the parser dict (keys `live_update`,
`check_rules`, `check_limits`, `limit_type`, `limit_sort`, `reverse_sort`,
`limit_value`, `match_checked_only`) and rebuilds `limit_sort` with
`0x80000000` when `reverse_sort` is set. `rules_from_parsed` rebuilds
`SmartPlaylistRule`/`RuleGroup` trees (recursive for `group`; group
`header_bytes` kept only when it is `bytes`/`bytearray`, else `None`;
defaults: group `action_id` 1, leaf `action_id` 0, `group_marker`
`SPL_GROUP_MARKER`), applies the `from_value`/`from_date` part of the
relative-date normalization (the `to_*` part happens at write time), keeps
`unk004` (default `SLST_DEFAULT_UNK004`) and maps an integer conjunction
`1` → `"OR"`, any other integer → `"AND"`; a string conjunction is kept
as-is.

### 4.6 `mhla_writer` — albums (MHSD type 4)

```python
def write_mhia(album_id: int, album_name: str, album_artist: str,
               sort_album_artist: str = "", podcast_url: str = "",
               show_name: str = "", is_compilation: bool = False,
               album_track_db_id: int = 0) -> bytes
def write_mhla(tracks, starting_index_for_album_id) ->
    tuple[bytes, dict[tuple[str, str], int], int]
def write_mhla_empty() -> bytes
```

MHIA: 88-byte header (`MHIA_HEADER_SIZE`), `child_count` = number of
non-empty MHOD children, `sql_id` = `random.getrandbits(64)` (must be
non-zero), `platform_flag` = 2, `album_compilation_flag` = 1 iff
`is_compilation`, `album_track_db_id` u64 @0x20 (min hdr 0x28);
children emitted in order: MHOD 200 (album name), 201 (album artist),
202 (sort album artist), 203 (podcast URL), 204 (show) — each skipped when
empty. `season_number`, `album_rating`, `unk0x29_rating_flag` default 0.

MHLA: `write_list_header(b'mhla', 92, album_count)` + concatenated MHIA.
Albums are grouped by `album_identity_from_track` and sorted by
`(album, album_artist or artist, show)`; IDs start at
`starting_index_for_album_id` and increase by 1. `album_map[(album,
album_artist)] = id`. Each member track gets `track.album_id = id`
(mutates the `TrackInfo` objects). Representative
`album_track_db_id` = first member's `db_track_id`. Per-group values:
`album_name = identity.album or ""`, `album_artist = identity.album_artist
or identity.artist or ""` (this pair is the `album_map` key); MHOD 202 sort
artist = first member with `sort_album_artist`, else first with
`sort_artist`; MHOD 203 = first member's non-empty `podcast_rss_url`;
MHOD 204 = `identity.show_name` or the first member's `show_name`;
`is_compilation` = **any** member has `compilation_flag` set. The returned
next id is one past the last assigned id.

### 4.7 `mhli_writer` — artists (MHSD type 8)

```python
def write_mhii_artist(artist_id: int, artist_name: str) -> bytes
def write_mhli(tracks, starting_index_for_artist_id) ->
    tuple[bytes, dict[str, int], int]
def write_mhli_empty() -> bytes
```

MHII: 80-byte header (`MHII_HEADER_SIZE`), `sql_id` random u64,
`platform_flag` = 2, exactly one child when the name is non-empty:
MHOD 300 (`MHOD_TYPE_ARTIST_NAME`).

MHLI: 92-byte list header + MHII items. Dedup key is `artist.lower()`;
**display casing = first occurrence**; assignment order is
`sorted(keys)` (lowercase lexicographic). Returns
`(bytes, {lower_name: artist_id}, next_id)`.

### 4.8 `mhip_writer` — playlist items

```python
def write_mhip(track_id: int, position: int = 0, mhip_id: int = 0,
               timestamp: int = 0, podcast_group_flag: int = 0,
               podcast_group_ref: int = 0, track_persistent_id: int = 0,
               mhip_persistent_id: int = 0) -> bytes
def write_mhod_position(position: int) -> bytes
def write_mhip_podcast_group(album_name: str, group_id: int) -> bytes
```

MHIP: 76-byte header (`MHIP_HEADER_SIZE`), `child_count = 1`, fields per
`MHIP_FIELDS` (§4.9): `group_id` (+0x14) = `mhip_id`, `group_id_ref`
(+0x20) = `podcast_group_ref`; `timestamp` goes through the Unix→Mac
transform, and the default `0` stays `0` on disk (the transform maps
non-positive values to 0). `position` is clamped to u32. Sole child: **MHOD type 100**
position record: total = 24 + 20 = 44 bytes, body = `position` u32 LE +
16 zero bytes (`MHOD100_POSITION_BODY_SIZE = 20`).

Group-header MHIP (podcast dataset 3): `podcast_group_flag = 256` (0x100),
`track_id = 0`, `group_id = group_id`, child = MHOD type 1 whose value is
the stripped album name or `"Unknown"` when empty.

### 4.9 `MHYP`, `MHIP`, `MHIA`, `MHII` field tables (verbatim)

**`MHYP_HEADER_SIZE = 184`:**

| Offset | Type | Field | min | Notes / default |
|---|---|---|---|---|
| 0x0C | u32 | `mhod_child_count` | | |
| 0x10 | u32 | `mhip_child_count` | | |
| 0x14 | u8 | `master_flag` | | type byte: 1 = master / builtin |
| 0x15 | u8 | `flag1` | | default 0 |
| 0x16 | u8 | `flag2` | | default 0 |
| 0x17 | u8 | `flag3` | | default 0 |
| 0x18 | u32 | `timestamp` | | Mac↔Unix |
| 0x1C | u64 | `playlist_id` | | |
| 0x24 | u32 | `unk0x24` | | |
| 0x28 | u16 | `string_mhod_child_count` | | |
| 0x2A | u16 | `playlist_kind_flags` | | bit0 podcast, 0x0100 folder |
| 0x2C | u32 | `sort_order` | | |
| 0x30 | u64 | `parent_folder_playlist_id` | | default 0 |
| 0x38 | u32 | `unk0x38` | | default 0 |
| 0x3C | u64 | `db_id_2` | 0x44 | non-master only |
| 0x44 | u64 | `playlist_id_2` | 0x4C | non-master only |
| 0x4C | u32 | `unk0x4C` | 0x50 | default 0 |
| 0x50 | u16 | `mhsd5_type` | 0x52 | |
| 0x52 | u16 | `phase_game_flag` | 0x54 | |
| 0x54 | u32 | `mhsd5_special_flag` | 0x58 | 1 when mhsd5_type ∈ {6,7} |
| 0x58 | u32 | `timestamp_2` | 0x5C | Mac↔Unix |

**`MHIP_HEADER_SIZE = 76`:**

| Offset | Type | Field | min |
|---|---|---|---|
| 0x0C | u32 | `child_count` | |
| 0x10 | u16 | `podcast_group_flag` | |
| 0x12 | u16 | `unk0x12` | |
| 0x14 | u32 | `group_id` | |
| 0x18 | u32 | `track_id` (required) | |
| 0x1C | u32 | `timestamp` | Mac↔Unix |
| 0x20 | u32 | `group_id_ref` | |
| 0x24 | u64 | `unk0x24_group_persistent_id` | default 0 |
| 0x2C | u64 | `track_persistent_id` | 0x34 |
| 0x3C | u64 | `mhip_persistent_id` | 0x44 |

**`MHIA_HEADER_SIZE = 88`:**

| Offset | Type | Field | min |
|---|---|---|---|
| 0x0C | u32 | `child_count` | |
| 0x10 | u32 | `album_id` (required) | |
| 0x14 | u64 | `sql_id` | |
| 0x1C | u16 | `platform_flag` (default 2) | |
| 0x1E | u16 | `album_compilation_flag` | |
| 0x20 | u64 | `album_track_db_id` | 0x28 |
| 0x28 | u8 | `album_rating` (default 0) | |
| 0x29 | u8 | `unk0x29_rating_flag` (default 0) | |
| 0x2C | u32 | `season_number` (default 0) | |

**`MHII_HEADER_SIZE = 80`:**

| Offset | Type | Field |
|---|---|---|
| 0x0C | u32 | `child_count` |
| 0x10 | u32 | `artist_id` (required) |
| 0x14 | u64 | `sql_id` |
| 0x1C | u32 | `platform_flag` (default 2) |

### 4.10 `mhlp_writer` — playlist lists

```python
def write_mhlp_empty() -> bytes                                  # count 0
def write_mhlp(playlist_chunks: list[bytes]) -> bytes
def write_mhlp_with_playlists(track_ids, playlists, db_id_2,
    tracks=None, capabilities=None, master_playlist_name="iPod",
    master_playlist_id=None) -> bytes
def write_mhlp_with_playlists_type3(track_ids, playlists, db_id_2,
    track_album_map, tracks=None, capabilities=None,
    master_playlist_name="iPod", next_mhip_id_start=1,
    master_playlist_id=None) -> bytes
def write_mhlp_smart(playlists, db_id_2=0) -> bytes
```

- Dataset 2 (`write_mhlp_with_playlists`): **master playlist first**
  (`write_master_playlist`), then the user playlists in the order
  `_reconcile_playlist_infos` returns them — **folder preorder** (chapter 03
  §7.4): input order for top-level rows, each folder immediately followed
  by its subtree. Without folders this is the input order.
- Dataset 3: identical master, then each reconciled playlist written with
  `podcast_grouping=True` and `track_album_map` / `next_mhip_id_start`
  (the same start value is passed to every playlist).
- Dataset 5 (`write_mhlp_smart`): **no master** is synthesized; rows are
  reconciled the same way; a row's own `master` flag (MHYP +0x14 = 1, which
  in dataset 5 means "built-in category") is written as given, with no
  single-master check. Empty input → `write_mhlp_empty()` (0 children).
- `_reconcile_playlist_infos(playlists)` applies
  `reconcile_playlist_hierarchy` (shared folder contract: parent links,
  folder prefs/rules synthesis, item filtering) and returns **new**
  `PlaylistInfo` objects via `dataclasses.replace`. Each playlist becomes a
  row `{playlist_id, playlist_kind_flags, podcast_flag, is_folder,
  is_podcast, parent_folder_playlist_id, items: [{"db_track_id": id}, …]}`;
  every result gets `track_ids` from the reconciled items (so duplicate ids
  inside one playlist are dropped), the normalized kind word in both
  `podcast_flag` and `playlist_kind_flags`, and the validated
  `parent_folder_playlist_id`. Folder rows get
  `smart_prefs` (existing or `prefs_from_parsed(row["smart_playlist_data"])`),
  `smart_rules` (`rules_from_parsed(...)`), and `item_metadata=None`.
  Caller-owned objects are never mutated.

### 4.11 `mhyp_writer` — playlist rows

```python
def generate_playlist_id() -> int           # random.getrandbits(64)
def write_mhyp(name, track_ids, playlist_id=None, master=False,
    timestamp=None, sortorder=0, podcast_flag=0, tracks=None, db_id_2=0,
    smart_prefs=None, smart_rules=None, mhsd5_type=0, phase_game_flag=0,
    raw_mhod100=None, raw_mhod102=None, raw_mhod55=None,
    playlist_description=None, item_metadata=None, capabilities=None,
    podcast_grouping=False, track_album_map=None, next_mhip_id_start=1,
    playlist_kind_flags=None, parent_folder_playlist_id=0) -> bytes
def write_playlist(playlist: PlaylistInfo, db_id_2=0, podcast_grouping=False,
    track_album_map=None, next_mhip_id_start=1) -> bytes
def write_master_playlist(track_ids, db_id_2, name="iPod", tracks=None,
    capabilities=None, playlist_id=None) -> bytes
def write_mhod_playlist_prefs() -> bytes
def _write_mhod100_raw(raw_body: bytes) -> bytes
```

Child layout, in order:

1. MHOD 1 — title (empty name → `"Playlist"`).
2. MHOD 3 — `playlist_description` (skipped when None/empty; counted in
   `string_mhod_child_count` as `1 + description_count`).
3. MHOD 100 — playlist prefs: `raw_mhod100` body passthrough, else the
   generated 0x288-byte default (below).
4. MHOD 50 + MHOD 51 — only when **both** `smart_prefs` and
   `smart_rules` are set.
5. MHOD 102 — only when `raw_mhod102` given.
6. MHOD 55 — only when `raw_mhod55` given.
7. MHOD 52/53 library indices — only when `master and tracks` (dataset 5
   never passes `tracks`).
8. MHIPs — flat (one per `track_id`, `position = i`) or podcast-grouped.
   Flat MHIP *i* takes `group_id`, `podcast_group_flag`, `podcast_group_ref`,
   `track_persistent_id`, `mhip_persistent_id` from `item_metadata[i]` when
   that entry exists; missing entries (shorter or absent list) write 0s.
   Timestamps of MHIPs are always 0.

`mhod_child_count` = `2 + description_count + smart(0 or 2) + settings +
property_plist + library_indices_count` (the constant 2 = title + MHOD 100).
`mhip_child_count` = `len(track_ids)` flat, or tracks + album-group headers
when grouped.

Header values written: `master_flag` = 1/0; `timestamp`/`timestamp_2` =
Unix now if `timestamp is None`; `playlist_id` (generated when None);
`playlist_kind_flags` = `playlist_kind_flags` arg if given else
`podcast_flag`; `parent_folder_playlist_id`; `sort_order` = `sortorder`.
**Only non-master rows** write `db_id_2` and `playlist_id_2` at 0x3C/0x44.
`mhsd5_type` written when non-zero; when ∈ {6, 7},
`mhsd5_special_flag` (0x54) = 1. `phase_game_flag` (0x52) =
`phase_game_flag or mhsd5_type` when either is non-zero.

Podcast grouping (`_build_podcast_grouped_mhips`, only when
`podcast_grouping and playlist.is_podcast and track_album_map is not None`):
tracks grouped by album preserving first-seen order; per group emit the
group-header MHIP then child MHIPs, IDs allocated consecutively starting at
`next_mhip_id_start` (`write_mhbd` passes `next_track_id`, i.e. one past the
last track ID). Child `position` = its own unique `mhip_id`;
`group_id_ref` = parent header's `group_id`; `mhip_child_count` = tracks +
groups.

`write_master_playlist` always calls `write_mhyp` with `master=True` and
`sortorder=5`, and includes library indices when `tracks` is provided.

Generated default MHOD 100 (`write_mhod_playlist_prefs`) — total
**0x288 (648) bytes**, header via `write_mhod_header(100, 0x288)`, body
zeros except these LE u32 words:

| Offset | 0x30 | 0x34 | 0x38 | 0x3C | 0x40 | 0x4C | 0x50 | 0x5C | 0x60 | 0x6C | 0x70 | 0x7C | 0x80 | 0x8C | 0x90 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Value | 0x010084 | 0x05 | 0x09 | 0x03 | 0x120001 | 0x640014 | 0x01 | 0x320014 | 0x01 | 0x5A0014 | 0x01 | 0x500014 | 0x01 | 0x7D0015 | 0x01 |

Words 0x18..0x2C are zero; everything after 0x90 is zero.
`_write_mhod100_raw(body)` = `write_mhod_header(100, 24+len(body)) + body`.

---

## 5. `write_mhbd` — assembling the database

```python
def write_mhbd(tracks, db_id=None, language="en", reference_info=None,
    playlists_type2=None, playlists_type3=None, playlists_type5=None,
    preserved_mhsd_blobs=None, capabilities=None,
    master_playlist_name="iPod", master_playlist_id=None,
    podcast_master_playlist_name=None, podcast_master_playlist_id=None,
    *, platform=None) -> bytes
```

Returns the complete file bytes (uncompressed regardless of target
filename). `generate_database_id()` = `random.getrandbits(64)`.

### 5.1 `db_version`

`ref = reference_info["version"]` (0 if absent),
`cap = capabilities.db_version` (0 if absent):

- `cap != 0` → `max(ref, cap)`
- else `ref != 0` → `ref`
- else `DATABASE_VERSION_DEFAULT = 0x4F`

This value controls MHBD `version`, MHIT header sizing (§4.2), and the
legacy dataset strip (§5.5).

### 5.2 Ordering of children inside `write_mhbd`

1. `write_mhla(tracks, starting_index_for_album_id=1)` → MHSD 4.
2. `write_mhli(tracks, starting_index_for_artist_id=last_id+1)` → MHSD 8.
3. Composer ID map — **no dataset**: first-seen order over `tracks`
   (skip empty composer), keyed `composer.lower()`, IDs continue from
   `last_id+1`.
4. Per-track assignment: `album_id` (from the album map when the track's
   own `album_id` is 0), `artist_id` (artist map, lowercase key),
   `composer_id` (composer map). Missing/empty names leave 0.
5. `write_mhlt(tracks, start_track_id=last_id+1, db_id_2, capabilities,
   db_version)` → MHSD 1. Track IDs are the contiguous range
   `[last_id+1, next_track_id)`.
6. Build `db_track_id → track_id` map (position `i` → `i + last_id + 1`,
   only for tracks with non-zero `db_track_id`).
7. Remap playlists (non-mutating, §5.4) and build MHSD 2
   (`write_mhlp_with_playlists`), MHSD 3 (`…_type3`), MHSD 5
   (`write_mhlp_smart`); empty stubs for MHSD 6 and 10.

ID ranges therefore: albums first (from 1), then artists, then composers,
then tracks — all 32-bit sequential; `db_id_2` (u64) goes into every MHIT
(+0x124) and non-master MHYP (+0x3C).

### 5.3 Album / artist / composer keys

| Entity | Dedup key | Order |
|---|---|---|
| albums | `(album, album_artist or artist, show)` identity, compared case-insensitively (chapter 03 §10); map key `(album, album_artist)` in the group's first-track spelling | sorted by `(album, album_artist, show)` |
| artists | `artist.lower()` | `sorted(lowercase keys)` |
| composers | `composer.lower()` | **first-seen order over tracks** |

### 5.4 Playlist remapping (non-mutating)

`PlaylistInfo.track_ids` contain `db_track_id`s. For every playlist the
writer builds a copy (`dataclasses.replace`) whose `track_ids` are the
32-bit sequential IDs; `db_track_id`s not present in this database are
**dropped**. If `item_metadata` is provided, entries are carried along by
index; after filtering, if `len(new_meta) != len(new_ids)`,
`item_metadata` is set to `None` (metadata dropped rather than misaligned).
The caller's objects are never modified, so `write_mhbd` can be retried.

### 5.5 Dataset assembly rules

Default order (no usable reference): **1, 3, 2, 4, 8, 6, 10, 5**
(type 3 between 1 and 2; type 1 first — older firmware assumes
`dataset[0]` is the track list).

- `playlists_type3 is None` → clone the (unmodified) `playlists_type2`
  input for dataset 3 (device default); `[]` is meaningful and
  produces only the generated dataset-3 master.
- Dataset 3 is omitted entirely when `capabilities` is given and
  `capabilities.supports_podcast` is false.
- Reference-driven writes: if `reference_info` carries `mhsd_types`
  containing `1` (otherwise the reference is ignored for this purpose),
  only referenced types are emitted. Required set: `{1, 2}` always, plus
  `{3}` when 3 is referenced and podcasts are included — i.e. type 2 is
  **always** written once a usable reference exists, even if the reference
  had only type 3 (or neither).
  - With a non-empty `mhsd_order`: walk it in order, skipping types the
    writer does not generate, type 3 when podcasts are excluded, legacy-strip
    types, and types neither referenced nor required; empty blobs are
    skipped. When type 3 is emitted and type 2 is required but was not
    referenced, type 2 is inserted **immediately after** it. Finally any
    still-missing required type is appended, checked in the order 1, 3, 2.
  - Without an order (only possible for direct `write_mhbd` callers —
    `write_itunesdb` always supplies one): the default order above, where
    1 and 4 are always written and 3/2/8/6/10/5 only when referenced; the
    required set is **not** consulted in this branch.
- Legacy strip: when `capabilities.db_version <= 0x19`, types **{6, 8, 10}**
  are never emitted (even if referenced).
- Empty MHSD data blobs (e.g. dataset 3 without podcasts) are skipped.
- `preserved_mhsd_blobs` (types not in `{1,2,3,4,5,6,8,10}` — i.e. 7 and 9)
  are appended **verbatim after** the generated datasets;
  `child_count` includes them. Total length = 244 + all dataset bytes.

`extract_preserved_mhsd_blobs(itdb_data) -> list[bytes]` walks MHSD
children of an existing database (decompressing iTunesCDB payloads first),
returns raw blobs whose type ∉ `{1,2,3,4,5,6,8,10}`, in file order; returns
`[]` for non-`mhbd` input. `extract_db_info(itdb_path) -> dict` reads the
first 244 bytes and returns `read_fields(..., "mhbd", header_length)`
(raises `ValueError(f"Not an iTunesDB file: {itdb_path}")` if the magic is
wrong). Keys use canonical field names (`db_id`, `db_id_2`,
`timezone_offset`, `hashing_scheme`, …).

### 5.6 Header write

`bytearray(244)` → generic header (`b"mhbd"`, 244, total) → `write_fields`
with the value map from §2.4 → return header + datasets.

---

## 6. Checksums and hashes

### 6.1 `ChecksumType` and detection

`podsync.device.checksum.ChecksumType(IntEnum)`:
`NONE=0, HASH58=1, HASH72=2, HASHAB=3, UNSUPPORTED=98, UNKNOWN=99`.

Wire mapping (`CHECKSUM_MHBD_SCHEME`): `NONE→0`, `HASH58→1`, `HASH72→2`,
**`HASHAB→4`** (enum 3, wire 4). `MHBD_SCHEME_TO_CHECKSUM` is its inverse.

Device → checksum family: pre-2007 (1G–5.5G, Mini, Photo, Nano 1G–2G,
Shuffle) → NONE; Classic all gens, Nano 3G/4G → HASH58; Nano 5G → HASH72;
Nano 6G/7G → HASHAB.

`detect_checksum_type(ipod_path)` (in `podsync.device.info`): centralized
device store (`checksum_type != 99`) → virtual iPod metadata → SysInfo
model lookup via `checksum_type_for_family_gen` → HashInfo file existence
⇒ `HASH72` (an `OSError` inspecting it raises `DeviceWriteSafetyError`)
→ otherwise `UNKNOWN` (`FileNotFoundError` reading SysInfo → `UNKNOWN`
directly). Never guesses `NONE` from empty metadata.

`get_firewire_id(ipod_path, *, known_guid=None) -> bytes` tries, in order:
caller hex GUID (non-zero), centralized store `firewire_id_bytes`, virtual
store, SysInfo `FirewireGuid` (with optional `0x` prefix, must be non-zero),
SysInfoExtended `FireWireGUID` plist key; raises `RuntimeError` if none
found.

### 6.2 `hash58` — HMAC-SHA1 at +0x58 (Classic, Nano 3G/4G)

Constants — **copy verbatim**:

```python
TABLE1 = bytes([   # AES S-box
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5,
    0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0,
    0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC,
    0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A,
    0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0,
    0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B,
    0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85,
    0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5,
    0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17,
    0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88,
    0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C,
    0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9,
    0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6,
    0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E,
    0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94,
    0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68,
    0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
])

TABLE2 = bytes([   # AES inverse S-box
    0x52, 0x09, 0x6A, 0xD5, 0x30, 0x36, 0xA5, 0x38,
    0xBF, 0x40, 0xA3, 0x9E, 0x81, 0xF3, 0xD7, 0xFB,
    0x7C, 0xE3, 0x39, 0x82, 0x9B, 0x2F, 0xFF, 0x87,
    0x34, 0x8E, 0x43, 0x44, 0xC4, 0xDE, 0xE9, 0xCB,
    0x54, 0x7B, 0x94, 0x32, 0xA6, 0xC2, 0x23, 0x3D,
    0xEE, 0x4C, 0x95, 0x0B, 0x42, 0xFA, 0xC3, 0x4E,
    0x08, 0x2E, 0xA1, 0x66, 0x28, 0xD9, 0x24, 0xB2,
    0x76, 0x5B, 0xA2, 0x49, 0x6D, 0x8B, 0xD1, 0x25,
    0x72, 0xF8, 0xF6, 0x64, 0x86, 0x68, 0x98, 0x16,
    0xD4, 0xA4, 0x5C, 0xCC, 0x5D, 0x65, 0xB6, 0x92,
    0x6C, 0x70, 0x48, 0x50, 0xFD, 0xED, 0xB9, 0xDA,
    0x5E, 0x15, 0x46, 0x57, 0xA7, 0x8D, 0x9D, 0x84,
    0x90, 0xD8, 0xAB, 0x00, 0x8C, 0xBC, 0xD3, 0x0A,
    0xF7, 0xE4, 0x58, 0x05, 0xB8, 0xB3, 0x45, 0x06,
    0xD0, 0x2C, 0x1E, 0x8F, 0xCA, 0x3F, 0x0F, 0x02,
    0xC1, 0xAF, 0xBD, 0x03, 0x01, 0x13, 0x8A, 0x6B,
    0x3A, 0x91, 0x11, 0x41, 0x4F, 0x67, 0xDC, 0xEA,
    0x97, 0xF2, 0xCF, 0xCE, 0xF0, 0xB4, 0xE6, 0x73,
    0x96, 0xAC, 0x74, 0x22, 0xE7, 0xAD, 0x35, 0x85,
    0xE2, 0xF9, 0x37, 0xE8, 0x1C, 0x75, 0xDF, 0x6E,
    0x47, 0xF1, 0x1A, 0x71, 0x1D, 0x29, 0xC5, 0x89,
    0x6F, 0xB7, 0x62, 0x0E, 0xAA, 0x18, 0xBE, 0x1B,
    0xFC, 0x56, 0x3E, 0x4B, 0xC6, 0xD2, 0x79, 0x20,
    0x9A, 0xDB, 0xC0, 0xFE, 0x78, 0xCD, 0x5A, 0xF4,
    0x1F, 0xDD, 0xA8, 0x33, 0x88, 0x07, 0xC7, 0x31,
    0xB1, 0x12, 0x10, 0x59, 0x27, 0x80, 0xEC, 0x5F,
    0x60, 0x51, 0x7F, 0xA9, 0x19, 0xB5, 0x4A, 0x0D,
    0x2D, 0xE5, 0x7A, 0x9F, 0x93, 0xC9, 0x9C, 0xEF,
    0xA0, 0xE0, 0x3B, 0x4D, 0xAE, 0x2A, 0xF5, 0xB0,
    0xC8, 0xEB, 0xBB, 0x3C, 0x83, 0x53, 0x99, 0x61,
    0x17, 0x2B, 0x04, 0x7E, 0xBA, 0x77, 0xD6, 0x26,
    0xE1, 0x69, 0x14, 0x63, 0x55, 0x21, 0x0C, 0x7D,
])

FIXED = bytes([
    0x67, 0x23, 0xFE, 0x30, 0x45, 0x33, 0xF8, 0x90, 0x99,
    0x21, 0x07, 0xC1, 0xD0, 0x12, 0xB2, 0xA1, 0x07, 0x81,
])

ITDB_CHECKSUM_HASH58 = 1
```

Key derivation (`_generate_key(firewire_id)`, requires ≥ 8 bytes, else
`ValueError(f"FireWire ID must be at least 8 bytes, got {len(firewire_id)}")`):

1. `y = bytearray(16)`; for `i in 0..3`: `a = fw[2i]`, `b = fw[2i+1]`,
   `l = lcm(a, b)` (`_lcm` returns **1** if either operand is 0),
   `hi = (l >> 8) & 0xFF`, `lo = l & 0xFF`;
   `y[4i]=TABLE1[hi]`, `y[4i+1]=TABLE2[hi]`,
   `y[4i+2]=TABLE1[lo]`, `y[4i+3]=TABLE2[lo]`.
2. `h = sha1(FIXED + y).digest()` (20 bytes), placed into a **64-byte
   zero buffer** → the HMAC key.

`compute_hash58(firewire_id, itdb_data) -> bytes` — manual HMAC-SHA1 over
the **entire file**: `inner = sha1((key ^ 0x36…) + data)`,
`outer = sha1((key ^ 0x5C…) + inner)` (i.e. block size 64, no extra
key padding needed since the derived key is already 64 bytes).

`write_hash58(itdb_data: bytearray, firewire_id: bytes) -> None`, in order:

1. Validate `len ≥ 0x6C` else `ValueError(f"iTunesDB file too small ({len} bytes), need at least 0x6C")`;
   magic `b"mhbd"` else `ValueError("Invalid iTunesDB: expected 'mhbd' header")`.
2. Back up `db_id` (8 B) and `unk0x32` (20 B).
3. Zero `db_id`, `unk0x32`, `hash58` (20 B).
4. Set `hashing_scheme` (0x30, u16 LE) = 1.
5. `hash = compute_hash58(firewire_id, bytes(itdb_data))` — must be 20
   bytes else `RuntimeError`; store at 0x58.
6. Restore `db_id` and `unk0x32`. (`hashing_scheme` stays 1.)

`read_firewire_id(ipod_path)` delegates to `podsync.device.get_firewire_id`.

### 6.3 `hash72` — AES signature at +0x72 (Nano 5G)

```python
AES_KEY = bytes([0x61, 0x8c, 0xa1, 0x0d, 0xc7, 0xf5, 0x7f, 0xd3,
                 0xb4, 0x72, 0x3e, 0x08, 0x15, 0x74, 0x63, 0xd7])
ITDB_CHECKSUM_HASH72 = 2
HASHINFO_HEADER = b"HASHv0"   # + uuid[20] + rndpart[12] + iv[16] = 54 bytes
```

`HashInfo` is a plain class with attributes `uuid: bytes` (20),
`rndpart: bytes` (12), `iv: bytes` (16).

SHA1 input (`_compute_itunesdb_sha1(itdb_data) -> bytes`): copy the buffer,
zero **`db_id` (0x18, 8), `hash58` (0x58, 20), `hash72` (0x72, 46)** —
`unk0x32` is **not** zeroed — then `sha1` over the whole copy.

Signature (`_hash_generate(sha1, iv, rndpart) -> bytes`, 46 bytes):

| Bytes | Content |
|---|---|
| 0..1 | `0x01 0x00` marker |
| 2..13 | `rndpart` (12 B) |
| 14..45 | AES-128-CBC (`AES_KEY`, `iv`) of `sha1 + rndpart` (32 B plaintext → 32 B ciphertext) |

Requires `pycryptodome` (`Crypto.Cipher.AES`, fallback `Cryptodome`),
else `ImportError("PyCryptodome is required for HASH72. Install with: pip install pycryptodome")`.

HashInfo file (`iPod_Control/Device/HashInfo`, exactly 54 bytes):
`read_hash_info(ipod_path) -> HashInfo | None` first consults the
centralized device store (`dev.hash_info_iv` + `dev.hash_info_rndpart`,
uuid filled with zeros); otherwise reads disk, returning `None` when the
file is missing, shorter than 54 bytes, or not `HASHv0`.
`write_hash_info(ipod_path, uuid, iv, rndpart, *, reported_volume_format="",
expected_volume_identity_key="") -> bool` returns `False` on wrong lengths,
otherwise writes atomically inside the guarded device-metadata session.

Recovery from an existing database:

- `_hash_extract(signature, sha1) -> (iv, rndpart) | None` decrypts the
  first ciphertext block with `sha1[:16]` as the CBC IV (recovering the
  real IV from the ECB-preimage of block 0) and validates the `01 00`
  marker and recovered `rndpart`.
- `extract_hash_info_to_dict(valid_itdb_data) -> {'iv':…, 'rndpart':…} | None`
  and `extract_hash_info(ipod_path, valid_itdb_data) -> bool` (the latter
  writes the file; uuid = FireWire id copied to the **start** of a 20-byte
  zero buffer, or 20 zero bytes when the id cannot be read).
  Both require `len ≥ 0xA0`, `mhbd` magic, and marker `01 00` at 0x72.

`compute_hash72(ipod_path, itdb_data) -> bytes` raises
`FileNotFoundError(f"HashInfo file not found at {path}. Sync once with iTunes to create it, or use extract_hash_info() with a valid iTunes-generated iTunesDB.")`
when no material is available.

`write_hash72(itdb_data, ipod_path) -> None`: validate `len ≥ 0x6C`
(`ValueError(f"iTunesDB file too small ({len} bytes)")`) and magic
(`ValueError("Invalid iTunesDB: expected 'mhbd' header")`); **set `hashing_scheme` = 2 first** (it is part of the SHA1 input);
then store the 46-byte signature at 0x72.

### 6.4 `hashab` — unsupported (Nano 6G/7G)

Nano 6G/7G databases carry a 57-byte signature at MHBD +0xAB computed with a
white-box AES implementation. podsync does **not** implement it (chapter 01
§5). The module exists only so imports and the package `__all__` stay
stable:

- `HASHAB_SIZE = 57`, `ITDB_CHECKSUM_HASHAB = 4` (the MHBD wire value).
- `compute_hashab(sha1_digest, uuid)` and `write_hashab(itdb_data, firewire_id)`
  raise `NotImplementedError("HASHAB signing (iPod nano 6G/7G) is not supported by podsync")`
  without touching `itdb_data`.
- No WASM module, no `wasmtime` import, no network download.

---

## 7. `write_itunesdb` — end-to-end install

```python
def write_itunesdb(
    ipod_path: str,
    tracks: list[TrackInfo],
    db_id: int | None = None,
    backup: bool = True,
    force_checksum: ChecksumType | None = None,
    firewire_id: bytes | None = None,
    reference_itdb_path: str | None = None,
    pc_file_paths: dict | None = None,
    playlists: list[PlaylistInfo] | None = None,
    podcast_playlists: list[PlaylistInfo] | None = None,
    smart_playlists: list[PlaylistInfo] | None = None,
    capabilities: DeviceCapabilities | None = None,
    master_playlist_name: str = "iPod",
    master_playlist_id: int | None = None,
    podcast_master_playlist_name: str | None = None,
    podcast_master_playlist_id: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
    before_database_replace: Callable[[], None] | None = None,
    before_device_mutation: Callable[[], None] | None = None,
) -> bool
```

Progress messages, in the order they are delivered to `progress_callback`:
`"Preparing database"` → artwork part → `"Building database structure"` →
`"Signing database"` → `"Writing to iPod"`. The artwork part is
`"Skipping artwork (no sources)"` when `pc_file_paths is None`; otherwise
it is the artwork writer's own messages (chapter 05 §12.9 table, forwarded
through the same callback), preceded by
`"Artwork — generating podsync-only artwork"` only in the no-artwork-device
fallback of step 6.

Steps (exceptions other than the two noted propagate **after** aborting any
pending artwork; the two noted return `False` instead):

1. **Capabilities** — if `capabilities is None`, look up the current device
   for `ipod_path` and derive them via `capabilities_for_family_gen(family,
   generation)`; any failure is swallowed (logged at debug).
2. **Filename selection** — `_database_filename_for_capabilities`:
   `None` when capabilities unknown, else `"iTunesCDB"` if
   `supports_compressed_db` else `"iTunesDB"`. `_resolve_existing_itdb_for_write`
   probes the preferred name and its counterpart (default order
   `iTunesCDB`, `iTunesDB`), preferring the first **non-empty** regular file;
   a non-regular path raises `OSError(f"iPod database path is not a regular file: {path}")`.
   Final `db_filename` = preferred → non-empty disk name → `"iTunesDB"`.
   Target path: `iPod_Control/iTunes/<filename>` resolved through the
   guarded device-path resolver.
3. **Read + validate existing DB** (if any): unreadable non-empty file →
   `RuntimeError(f"The existing iPod database could not be read safely: {path}. podsync stopped before replacing it: {exc}")`;
   zero bytes read from a non-empty file →
   `RuntimeError("The existing iPod database became empty while it was being read. podsync stopped before replacing it.")`;
   then `_validate_existing_itunesdb`: magic/length ≥ 244 else
   `RuntimeError(f"The existing iPod database is truncated or malformed: {path}. podsync stopped before replacing it.")`;
   invalid `header_len`/`total_len` (`header < 244`,
   `header > total`, `total > file size`) → `…"has invalid size fields: {path}. podsync stopped before replacing it."`;
   `cdb_flag == 1` requires the payload to zlib-decompress else
   `…"The existing compressed iPod database is corrupt: {path}. podsync stopped before replacing it."`.
4. **Reference material** — `reference_itdb_path` read (best effort);
   `db_id` preserved from existing bytes `[24:32]` when not given;
   `reference_info = read_fields(source, 0, "mhbd", header_len)` plus
   `mhsd_types` (set), `mhsd_order` (ordered list, deduped, walked on the
   **decompressed** view) and `mhit_header_size` (first MHIT of dataset 1).
   Extraction failure → logged warning, `reference_info = None`.
   Platform: existing DB flag (if 1/2, source `existing_database`) →
   reference DB flag (source `reference_database`) → none; combined with
   the filesystem type via `resolve_itunesdb_platform` (chapter 07 §5); when
   a database flag was used, the resolution's source is replaced by that
   evidence source. Always logs INFO
   `iTunesDB platform selection: flag=<1|2> (<Mac|Windows>) source=<source> filesystem=<type or unknown> reference=<flag or none>`.
   A mismatch between the kept flag and the filesystem is not fatal and logs
   WARNING
   `iTunesDB platform/filesystem mismatch: preserving flag=<n> (<name>) from <the existing on-device database | the supplied reference database> although filesystem=<type> suggests <Mac|Windows>`.
5. **Track IDs** — every track with `db_track_id == 0` gets
   `generate_db_track_id()` (before artwork, which keys on it).
6. **Artwork** — only when `pc_file_paths is not None` (an empty dict still
   enters this branch). Formats: `artwork_formats = None` (the artwork
   writer resolves them for the device), **except** when `capabilities` is
   known and `supports_artwork` is false: then
   `artwork_formats = {1060: (w, h)}` from `ITHMB_FORMAT_MAP[1060]`
   (320×320), progress `"Artwork — generating podsync-only artwork"`, INFO
   `ART: device reports no artwork support; writing fallback format 1060 for podsync view`
   (a failure to resolve 1060 only logs WARNING
   `ART: could not resolve fallback artwork format: {exc}` and keeps `None`).
   `reference_artdb_path` = `<ipod>/iPod_Control/Artwork/ArtworkDB` when it
   exists. Lazy-import and call `write_artworkdb(ipod_path, tracks,
   pc_file_paths, reference_artdb_path, artwork_formats, defer_commit=True,
   progress_callback, before_device_mutation=<both hooks, step 12>)`; the
   result may be a `PendingArtworkWrite` or a plain dict (then nothing is
   pending). On success every track's
   `mhii_link`/`artwork_count`/`artwork_size` is set from the returned map
   or **cleared to 0** when absent (artwork state converges to the map);
   a `PendingArtworkWrite` is kept for later commit/abort. Any exception:
   abort pending artwork, log, **re-raise**.
   Otherwise progress `"Skipping artwork (no sources)"`.
7. **Build** — `preserved_blobs = extract_preserved_mhsd_blobs(existing)`;
   `device_time_context = read_device_time_context(ipod_path,
   database_offset=reference_info.get("timezone_offset"))` (`None` when
   there is no reference); inside
   `use_device_time_context(...)` call `write_mhbd(...)` with the playlists
   (`playlists` → dataset 2, `podcast_playlists` → 3, `smart_playlists` →
   5) and `platform=platform_resolution.flag`. All MHBD/MHIT/MHYP timestamps
   are computed in the **device's** clock context.
8. **Compress (iTunesCDB only)** — *before* signing, because firmware hashes
   the on-disk bytes: keep the 244-byte header, `zlib.compress(payload, 1)`;
   patch total length at +8 to the compressed size; set `cdb_flag`
   (u16 at 0xA8) = 1. The compressed buffer is what gets signed.
9. **Sign** — checksum selection: `force_checksum` →
   `capabilities.checksum` (when not `UNKNOWN`) → `detect_checksum_type`.
   When detection yields `NONE` but the source DB (≥ 0xA0 bytes) exists,
   infer: `scheme==1` with non-zero hash58 **and** `01 00` marker at 0x72 →
   HASH58; marker at 0x72 alone → HASH72; `scheme==1` → HASH58;
   `scheme==2` → HASH72.
   - **HASH58 branch**: pack `hashing_scheme = 1` **first**; then, if the
     source has the `01 00` marker at 0x72, write a HASH72 signature
     *before* HASH58 (extracted IV/rndpart via
     `extract_hash_info_to_dict`; hash72's SHA1 zeros hash72 itself, while
     hash58's HMAC includes hash72 — so hash72 must exist first). Then
     `write_hash58` with `firewire_id` (argument, else
     `get_firewire_id(ipod_path)`); missing → `hash_error` = `"No FireWire ID is available to compute the required HASH58 signature. podsync stopped before writing a database the iPod firmware would reject."`
   - **HASH72 branch**: obtain material from the centralized store
     (`HashInfo(zeros, rndpart, iv)`), then `read_hash_info`, then
     reference extraction (`extract_hash_info_to_dict` → compute
     inline); pack `hashing_scheme = 2` **before** computing (it is part of
     the SHA1 input); nothing worked → `hash_error` = `"No valid HashInfo material is available to compute the required HASH72 signature. podsync stopped before writing a database the iPod firmware would reject."`.
     No hash58 is written in this branch.
   - **HASHAB branch**: `hash_error = "HASHAB signing (iPod nano 6G/7G) is not supported by podsync"`
     — no signing is attempted.
   - **UNSUPPORTED** → `hash_error = "Device requires an unsupported hashing scheme"`.
   - **UNKNOWN** → `hash_error = "Cannot write iTunesDB: device checksum type is UNKNOWN. The device was not fully identified — the iPod will reject this database. Please report this as a bug."`
   - **NONE** → pack `hashing_scheme = 0`.
   Any `hash_error`: log error, abort pending artwork (passing
   `before_device_mutation`), **return `False`** (no exception).
10. **Preflight** — run `before_device_mutation`, then
    `_preflight_database_install(ipod_path, itdb_path, len(data),
    capabilities, backup_sources=(itdb_path, existing_itdb_path) if backup else ())`:
    `profile = inspect_device_write_readiness(ipod_path)`; limit =
    `effective_max_file_size_bytes(profile.max_file_size_bytes,
    capabilities.max_database_bytes or 0)` (firmware limit `None` when
    capabilities are unknown); `require_file_size_supported(size,
    max_file_size_bytes=limit, display_name=<basename or "iTunes database">)`
    (raises `FileSizeLimitError`). Required free space =
    `allocated_size(size, profile.allocation_unit_size)` plus, per distinct
    existing backup source (deduped by normalized real path; missing files
    skipped), its allocated size. Errors, all `DeviceWriteSafetyError`:
    `stat` failure on a backup source →
    `"Could not verify space needed to back up the existing iPod database: {exc}"`;
    `disk_usage` failure →
    `"Could not verify iPod free space before writing the database: {exc}"`;
    too little space →
    `"The iPod does not have enough free space to stage and safely commit its database. At least {n:,} bytes are required, but only {m:,} bytes are available. podsync stopped before replacing the database."`.
    On `FileSizeLimitError` the exception is re-raised **after** attaching
    `exc.proposed_database_bytes = bytes(itdb_data)` and
    `exc.proposed_database_filename = db_filename` (artwork aborted first).
    Any other exception also aborts artwork and re-raises.
11. **Backup** (`backup=True`) — for each of the target path and the
    previously-existing path (deduped), copy to `<path>.backup` via
    `_copy_device_file_durably` (sibling temp + flush + atomic replace;
    detects mid-copy source mutation → `RuntimeError(f"Source changed while backing up {source}")`).
    Backup failure: log error, abort artwork, **return `False`**.
12. **Commit** — progress `"Writing to iPod"`; run `before_device_mutation`;
    write to a unique sibling temp file, flush; `pending_artwork.commit`
    (artwork lands **before** the database swap); run both mutation hooks
    (`before_device_mutation` then `before_database_replace`) as
    `_before_precommit_mutation`; `durable_replace(temp, target)`. If the
    filename changed (iTunesDB ↔ iTunesCDB), the stale file is replaced by
    a **0-byte** file (truncated, not deleted — firmware checks existence).
    Return `True`.
13. **Exception path** — log, remove any temps (guarded unlink), abort
    uncommitted artwork, **return `False`**.

Side effects on success: `<db>.backup` copies (when `backup`),
ArtworkDB/ithmb writes (when `pc_file_paths` given), the database file
replaced atomically, stale database truncated to 0 bytes. Never partially
writes the live database — the only writes before the final rename are temp
files inside the same directory.

---

## 8. Errors, edge cases, guarantees

| Condition | Behavior |
|---|---|
| `tracks == []` | valid: MHLT with 0 children, master playlist with empty `track_ids`, all lists well-formed |
| `write_mhbd([], playlists_type2=…, playlists_type3=…, playlists_type5=…)` | parseable file; byte-walkable by the companion reader |
| Empty string field on a track/playlist | MHOD omitted entirely (no zero-length chunk) |
| Oversized string | silently truncated to 4096 (8192 for comment/description/lyrics; 4096 UTF-8 for URLs); debug log |
| Track `location` missing/invalid | `ValueError` (message in §4.2), raised through `write_mhlt` with track context appended |
| Invalid field value (validator) | `InvalidFieldValueError` |
| Missing required field in a values dict | `MissingRequiredFieldError` |
| `RuleGroup.header_bytes` wrong size | `ValueError("RuleGroup.header_bytes must contain exactly 40 bytes")` |
| Chapters invalid / > 500 / suspicious titles | chapter MHOD skipped (whole list) |
| Playlist references unknown `db_track_id` | silently dropped during remap |
| `item_metadata` length mismatch after remap | metadata dropped (`None`) |
| `write_itunesdb` hash material missing (HASH58/72) or HASHAB device | returns `False`, artwork aborted, device untouched |
| `write_itunesdb` preflight/IO failure | artwork aborted, exception raised (preflight) or `False` (backup/write) |
| Checksum unsupported/unknown | `hash_error` → `False` |
| `write_checksum` unsupported enum | `ValueError(f"Unsupported checksum type: {checksum_type}.")` |
| `compute_hash72` without HashInfo | `FileNotFoundError` |
| `compute_hashab` / `write_hashab` called directly | `NotImplementedError` |

Ordering guarantees:

- Track order in MHLT = input order; track IDs strictly increasing from the
  first free ID.
- MHOD order inside MHIT fixed (§4.3 table); MHOD/MHIP order inside MHYP
  fixed (§4.11); master playlist always index 0 of MHLP (datasets 2/3).
- Datasets: reference `mhsd_order` honored, else `1,3,2,4,8,6,10,5`;
  preserved blobs last.
- Albums sorted; artists sorted by lowercase name; composers first-seen.

Round-trip guarantees (what the reader must be able to reproduce):
every chunk emitted here is re-readable by the companion parser — header
sizes, `child_count`s, string encodings/lengths, SPL big-endian layouts,
opaque passthroughs (`raw_mhod100/102/55`, preserved MHSD blobs), and
Mac↔Unix timestamp transforms all invert. Input objects (`TrackInfo`,
`PlaylistInfo`) are **never mutated** by `write_mhbd`/`write_mhlp_*`
except the documented back-fills on `TrackInfo`: `write_mhit`
(`db_track_id`, `date_added`), `write_mhla` (`album_id`), and `write_mhbd`
itself (`artist_id`, `composer_id`, and `album_id` for any track still at
0). `write_itunesdb` additionally fills `db_track_id` up front and, when
artwork runs, overwrites `mhii_link`, `artwork_count`, `artwork_size`.
`PlaylistInfo` objects are never touched (remapping and reconciliation
work on copies).

---

## 9. Explicitly out of scope

- SQLite nano 5G–7G write path — **absent** from this package; callers must
  get a clear error elsewhere, not a silent fallback to binary iTunesDB.
- `pc_track_to_info` (track conversion from PC metadata).
- `commit_playcounts_if_needed` (play-counts DB flush).
- Anything importing gui / application / podcasts / `sync.transcoder` /
  `sqlitedb_writer`.
- Artwork writing internals — only the deferred commit/abort contract of
  `write_artworkdb`/`PendingArtworkWrite` as used in §7 step 6.
- HASHAB signing (§6.4).
- Provenance: the `hash58`/`hash72` constants are public format facts
  (TABLE1/TABLE2 are the standard AES S-box and its inverse, FIPS-197);
  no provenance names appear in the public API or messages.

---

## 10. Relation to `01-overview.md`

Chapter 01 and this chapter agree: the generic header is the 12-byte
`"<4sII"` record of §2.1; HASH58 = iPod Classic + Nano 3G/4G, HASH72 =
Nano 5G, HASHAB (Nano 6G/7G) is refused (§6.4); pre-2007 models (including
the iPod Video 5G/5.5G) need no hash.
