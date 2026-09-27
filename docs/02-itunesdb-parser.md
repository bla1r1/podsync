# 02 — Reading iTunesDB: `podsync.itunesdb_parser`

This chapter specifies every module of `podsync.itunesdb_parser`: the
chunk-tree parser for Apple's iTunesDB (and its zlib variant iTunesCDB),
the sidecar files the firmware writes (`Play Counts`, `OTGPlaylistInfo*`),
the artwork-link hydrator, and the forensic byte-walk exporter/indexer.
Byte layouts come from `podsync.itunesdb_shared.*_defs`; where a table is
needed to interpret parser output it is duplicated here verbatim so the
chapter stands alone.

Everything is little-endian unless a section explicitly says
big-endian (only MHOD type 51 `SLst` rules and MHOD type 17 chapter
atoms are BE). All module loggers are `logging.getLogger(__name__)`,
i.e. named `podsync.itunesdb_parser.<module>`.

## 1. Module map and package exports

| Module | Role |
|---|---|
| `podsync.itunesdb_parser` (`__init__.py`) | public re-exports |
| `podsync.itunesdb_parser.exceptions` | parse-error hierarchy |
| `podsync.itunesdb_parser._parsing` | struct helpers, generic header, raw-chunk preservation |
| `podsync.itunesdb_parser.chunk_parser` | dispatcher, child iteration, unknown-chunk summary |
| `podsync.itunesdb_parser.parser` | `parse_itunesdb`, `decompress_itunescdb` |
| `podsync.itunesdb_parser.mhbd_parser` | database header + MHSD children |
| `podsync.itunesdb_parser.mhsd_parser` | dataset header + its single child |
| `podsync.itunesdb_parser.mhit_parser` | track item + MHOD children |
| `podsync.itunesdb_parser.mhod_parser` | every MHOD body family |
| `podsync.itunesdb_parser.mhyp_parser` | playlist header, MHOD + MHIP groups |
| `podsync.itunesdb_parser.mhip_parser` | playlist item (track reference) + MHOD children |
| `podsync.itunesdb_parser.mhia_parser` | album item + MHOD children |
| `podsync.itunesdb_parser.mhii_parser` | artist item + MHOD children |
| `podsync.itunesdb_parser.playcounts` | `Play Counts` parse + in-place merge |
| `podsync.itunesdb_parser.otg` | On-The-Go playlist discovery/import/delete |
| `podsync.itunesdb_parser.artwork_links` | track `artwork_id_ref` hydration from ArtworkDB |
| `podsync.itunesdb_parser.forensics` | lossless byte-walk JSON document |
| `podsync.itunesdb_parser.byte_walk` | low-memory index/load of byte-walk JSON |
| `podsync.itunesdb_parser.ipod_library` | whole-library convenience loader |

`podsync.itunesdb_parser.__init__` re-exports exactly (order as in
`__all__`):

| Name | Origin |
|---|---|
| `parse_itunesdb` | `parser` |
| `decompress_itunescdb` | `parser` |
| `parse_playcounts` | `playcounts` |
| `merge_playcounts` | `playcounts` |
| `PlayCountEntry` | `playcounts` |
| `ITunesDBParseError` | `exceptions` |
| `CorruptHeaderError` | `exceptions` |
| `UnknownChunkTypeError` | `exceptions` |
| `InsufficientDataError` | `exceptions` |

Import style matters for tests that patch attributes:

* `parser.parse_itunesdb` imports `chunk_parser.parse_chunk`,
  `reset_unknown_chunk_summary`, `log_unknown_chunk_summary` **inside
  the function body** (call-time lookup through the `chunk_parser`
  module), so patching `podsync.itunesdb_parser.chunk_parser.parse_chunk`
  affects `parse_itunesdb`.
* `ipod_library` binds `parse_itunesdb` **at module import time**
  (`from .parser import parse_itunesdb`), but imports
  `device_time` helpers, `artwork_links.hydrate_track_artwork_refs`,
  `otg.load_otg_playlists`, and the `playcounts` functions **inside the
  function bodies**, so patching
  `podsync.itunesdb_parser.artwork_links.hydrate_track_artwork_refs`,
  `podsync.itunesdb_parser.otg.load_otg_playlists`,
  `podsync.itunesdb_parser.playcounts.parse_playcounts`, … takes effect
  on the next call.
* Importing any module of the package must have **no side effects**
  (no file or network access at import time; stdlib only).

There is no dedicated `mhli_parser.py`: pure list chunks (`mhlt`,
`mhla`, `mhli`, `mhlp`) are handled inside `chunk_parser` (§4.3), and
nothing in this chapter imports anything else for them.

## 2. Binary fundamentals

### 2.1 Generic chunk header

Every chunk starts with one 12-byte record (struct `"<4sII"`,
`GENERIC_HEADER_SIZE == 12`):

| Off | Size | Type | Meaning |
|---|---|---|---|
| 0x00 | 4 | ASCII | chunk tag, e.g. `mhbd`, `mhit` |
| 0x04 | 4 | u32 LE | `header_length` — bytes to end of this chunk's header |
| 0x08 | 4 | u32 LE | `length_or_children` — semantics per tag (table below) |

| Tag | +0x08 means | Parser end rule |
|---|---|---|
| `mhbd` | total file length | `next_offset = offset + length` |
| `mhsd` | total dataset length | `next_offset = offset + length` |
| `mhit`, `mhod`, `mhyp`, `mhip`, `mhia`, `mhii` | total chunk length (header + body) | `next_offset = offset + length` |
| `mhlt`, `mhla`, `mhli`, `mhlp` | **child count** | `next_offset = end of the parsed children` |
| anything else | treated as a byte length (may actually be a count; see §4.4) | `next_offset = offset + length` |

Typical header sizes: `mhbd` 244 (0xF4) writer-side; `mhsd` 96; `mhlt/
mhla/mhli/mhlp` 92; `mhod` 24; `mhit` up to 624 (0x270); `mhyp` 184;
`mhip` 76; `mhia` 88; `mhii` 80. The parser always trusts the *actual*
`header_length` in the file and gates extended fields by it
(`min_header_length`, see §2.3).

### 2.2 Child containers

`mhbd` declares `child_count` (its field at 0x14) and is followed by
that many `mhsd` chunks. Each `mhsd` has **exactly one** child — the
list chunk selected by `dataset_type` (§6.2). List chunks
(`mhlt/mhla/mhli/mhlp`) hold `length_or_children` item chunks.
`mhit`/`mhip`/`mhia`/`mhii` hold `child_count` (their field at 0x0C)
`mhod` children. `mhyp` holds `mhod_child_count` (0x0C) MHODs
**first**, then `mhip_child_count` (0x10) MHIPs, parsed sequentially
from one running offset.

### 2.3 Field reads, defaults, transforms

All typed headers are described by `FieldDef` tables registered in
`podsync.itunesdb_shared` (`FIELD_REGISTRY`). Read rules the parser
must implement:

* A field with `min_header_length = M` reads its default when the
  chunk's `header_length < M`; otherwise it is unpacked at
  `chunk_offset + field.offset`.
* Defaults are `0` for numbers, `b"\x00" * size` for raw fields,
  unless the table below states another default (`visible=1`,
  `audio_format_flag=0xFFFF`, `media_type=1`, `platform=2`,
  `language=b"en"`, `unk0x50=1`, `unk0x54=15`, `unk0x168=1`, …).
* `read_transform`s applied at parse time:
  * Mac-epoch timestamps → Unix seconds via the active
    `DeviceTimeContext` (§14.5; chapter 03): `last_modified`, `last_played`,
    `date_added`, `date_released`, `date_added_to_itunes`,
    `last_skipped` (MHIT), `timestamp`/`timestamp_2` (MHYP),
    `timestamp` (MHIP).
  * `sample_rate_1` (MHIT 0x3C): raw u32 is 16.16 fixed; read value is
    `raw >> 16` (Hz).
* `required=True` affects **writes only**; on read a missing/short
  field simply takes the default (or `struct.error` if the buffer ends,
  §15).
* Validators (`rating` 0–100, `volume` −255..255) apply on write only.

### 2.4 Encodings

| Where | Encoding | Length rule |
|---|---|---|
| Standard string MHOD, sub-header encoding == 2 | UTF-8 | `string_length` bytes read at 0x28 |
| Standard string MHOD, encoding 0 or 1 (or anything ≠ 2) | UTF-16LE | `string_length` bytes read at 0x28 |
| Podcast URL MHOD (types 15/16) | UTF-8, then `rstrip("\x00")` | whole body after the 24-byte header |
| Chapter title (type 17 `name` atom) | UTF-16BE | `string_length` u16 code units |
| SLst string rule | UTF-16BE | `data_length` bytes |
| MHOD type 32 blob | hex string of raw bytes | whole body |
| Chunk tags | ASCII (non-ASCII → `CorruptHeaderError`) | 4 bytes |

All decodes use `errors="replace"`.

## 3. `podsync.itunesdb_parser.exceptions`

| Class | Base | `__init__` | Attributes | Message format |
|---|---|---|---|---|
| `ITunesDBParseError` | `Exception` | `()` | — | default |
| `CorruptHeaderError` | `ITunesDBParseError` | `(offset: int, detail: str)` | `.offset`, `.detail` | `f"Corrupt header at offset 0x{offset:X}: {detail}"` |
| `UnknownChunkTypeError` | `ITunesDBParseError` | `(offset: int, chunk_type: str)` | `.offset`, `.chunk_type` | `f"Unknown chunk type {chunk_type!r} at offset 0x{offset:X}"` |
| `InsufficientDataError` | `ITunesDBParseError` | `(offset: int, needed: int, available: int)` | `.offset`, `.needed`, `.available` | `f"Insufficient data at offset 0x{offset:X}: need {needed} bytes, only {available} available"` |

Raise conditions inside this package:

| Exception | Raised by | Condition | `detail` / args |
|---|---|---|---|
| `CorruptHeaderError` | `parser.parse_itunesdb` | input buffer is empty | `(0, "empty file")` |
| `CorruptHeaderError` | `_parsing.read_generic_header` | tag bytes not decodable as ASCII | `(offset, f"chunk type bytes are not valid ASCII: {raw_type!r}")` |
| `InsufficientDataError` | `_parsing.read_generic_header` | fewer than 12 bytes remain from `offset` | `(offset, 12, len(data) - offset)` |
| `UnknownChunkTypeError` | — | **never raised by this package** | exported for callers' catch-alls; unknown chunks are counted and skipped (§4.4) |

`struct.error` (from `struct.unpack_from`) is *not* wrapped: a chunk
whose declared `header_length` exceeds the buffer surfaces as
`struct.error` (see §15). `parse_itunesdb` does not catch parse
errors; only `ipod_library.load_ipod_library` converts any failure
into `None` (§14).

## 4. `podsync.itunesdb_parser.chunk_parser`

### 4.1 `parse_chunk(data: bytes | bytearray, offset: int) -> tuple[dict, str]`

Reads the generic header at `offset` (§2.1; may raise the two header
exceptions) and dispatches:

| Tag | Handler | Result `data` shape |
|---|---|---|
| `mhbd` | `mhbd_parser.parse_db` | dict (header fields + `children`) |
| `mhsd` | `mhsd_parser.parse_dataset` | dict (header fields + `children`, + payload keys for type 9) |
| `mhlt`, `mhla`, `mhli`, `mhlp` | internal pure-list parse (§4.3) | **list** of child wrappers |
| `mhit` | `mhit_parser.parse_track_item` | dict + `children` |
| `mhyp` | `mhyp_parser.parse_playlist` | dict + `mhod_children`, `mhip_children` |
| `mhip` | `mhip_parser.parse_playlist_item` | dict + `children` |
| `mhod` | `mhod_parser.parse_mhod` | dict (§7) |
| `mhia` | `mhia_parser.parse_album_item` | dict + `children` |
| `mhii` | `mhii_parser.parse_artist_item` | dict + `children` (iTunesDB context: artist item) |
| other | unknown path (§4.4) | `{"chunk_type", "header", "body"}` |

Return value: `(result, chunk_type)` where `result` always has
`"next_offset": int` and `"data": dict | list`, plus
`"_raw_chunk": dict` **only when raw preservation is active** (§5).
The internal `"_body_end"` marker is popped before returning; it only
feeds `_raw_chunk["unparsed_bytes"]`.

Typed handlers are imported lazily inside the `match` arms.

### 4.2 `parse_children(data, offset, child_count) -> tuple[list[dict], int]`

Parses `child_count` consecutive chunks starting at `offset`. Returns
`(children, next_offset)`; each child is
`{"chunk_type": str, "data": <parsed data>}` and, when preservation is
on, an extra `"_raw_chunk"` key copied from the child's result. A child
tag read past the buffer raises `InsufficientDataError`.

### 4.3 Pure-list containers

`_parse_child_list(data, offset, header_length, child_count)` returns
`{"next_offset", "data", "_body_end"}` with `data` = the children list
from `parse_children` starting at `offset + header_length`, and both
`next_offset` and `_body_end` = end of the last child. The 92-byte
list header itself carries no parsed fields (only the generic header;
the rest is zero padding). Bytes after the last child are *not*
consumed here — for a list chunk the third generic field is a count,
not a length.

### 4.4 Unknown chunks

On an unrecognized tag the parser:

1. increments module-level `Counter[(chunk_type, offset)]`;
2. sets `next_offset = offset + length_or_children` (treats the third
   field as a byte length even if it is a child count — worst case it
   skips too little and the parent's loop re-reads at the wrong spot,
   which surfaces as `CorruptHeaderError`/`InsufficientDataError` or a
   mis-detected tag; documented caveat, not an exception path);
3. builds `data = {"chunk_type": <str>, "header": bytes(offset..offset+header_length), "body": bytes(offset+header_length..offset+length)}` (slices clamp at the buffer end);
4. records `_body_end = offset + header_length`, so under preservation
   the whole unknown body lands in `unparsed_bytes`.

No log record is emitted per unknown chunk during parsing.

### 4.5 Unknown-chunk summary

| Function | Behavior |
|---|---|
| `reset_unknown_chunk_summary() -> None` | clears the Counter (called by `parse_itunesdb` before each top-level parse) |
| `log_unknown_chunk_summary() -> None` | no-op when the Counter is empty; otherwise logs **WARNING** |

Message format (exactly):

```
iTunesDB contained {total} unknown chunk(s); ignored while parsing. Examples: {examples}{suffix}
```

* `total = sum(counts.values())` (so parsing the same offset twice
  counts twice);
* `examples` = up to 5 entries from `Counter.most_common(5)` formatted
  as `f"{chunk_type!r} at 0x{offset:X}"`, joined by `", "`;
* `suffix` = `""` when the Counter has ≤ 5 distinct keys, else
  `f"; +{len(counts) - 5} more"`.

## 5. `podsync.itunesdb_parser._parsing`

| Name | Spec |
|---|---|
| `UINT16_LE/UINT32_LE/UINT64_LE/INT32_LE/FLOAT32_LE` | precompiled `struct.Struct` for `<H`, `<I`, `<Q`, `<i`, `<f` |
| `ParseResult` | type alias `dict[str, Any]` — every typed parser returns `{"next_offset": int, "data": ...}` (+ optional `_body_end` internally) |
| `preserve_raw_chunks(enabled: bool)` | context manager; sets a `ContextVar[bool]` (default `False`) with token reset — nestable, thread-context safe |
| `raw_chunk_preservation_enabled() -> bool` | reads that ContextVar |
| `read_generic_header(data, offset) -> tuple[str, int, int]` | `(chunk_type, header_length, length_or_children)`; §3 raise conditions |
| `raw_chunk_metadata(data, *, offset, header_length, declared_length_or_child_count, end_offset, parsed_body_end) -> dict` | see below |

`raw_chunk_metadata` returns exactly these keys:

| Key | Value |
|---|---|
| `offset` | chunk start |
| `header_length` | declared header length |
| `declared_length_or_child_count` | raw third field |
| `end_offset` | `min(max(offset, end_offset), len(data))` |
| `raw_header` | `data[offset : actual_header_end]` — every header byte incl. padding |
| `unparsed_bytes` | `data[actual_body_end : actual_end]` where `actual_body_end = clamp(parsed_body_end)` — container trailer, leaf body, or unknown body |

Clamps guarantee `offset ≤ header_end ≤ body_end ≤ end_offset ≤
len(data)` with all bounds lower-bounded by `offset`. Together with
child records these spans cover every byte of the file exactly once
(no container duplicates child bodies).

Per-parser `_body_end` choices (they define `unparsed_bytes`):

| Parser | `_body_end` | Consequence under `preserve_raw=True` |
|---|---|---|
| typed containers (`mhbd`, `mhsd`, `mhit`, `mhyp`, `mhip`, `mhia`, `mhii`) | end of parsed children | `unparsed_bytes` = trailer after the last child (empty when clean) |
| pure lists (`mhlt/mhla/mhli/mhlp`) | end of children | trailer after last child |
| `mhod` (all types) | `offset + header_length` | whole MHOD body, including decoded strings, stays byte-exact in `unparsed_bytes` |
| unknown chunk | `offset + header_length` | whole unknown body |
| `mhsd` type 9 | `offset + header_length` | payload bytes preserved (also duplicated logically into `raw_payload`) |

## 6. Typed container parsers

All seven share this contract:
`f(data: bytes | bytearray, offset: int, header_length: int, chunk_length: int) -> ParseResult`,
`data_dict = idb.read_fields(...)`, children from `parse_children`,
`next_offset = offset + chunk_length`, `_body_end` per the table above.

### 6.1 `mhbd_parser.parse_db` — database header

Children: `parse_children(data, offset + header_length, mhbd["child_count"])`.
`child_count` comes from field 0x14 (0 when the header is truncated
below it).

MHBD fields (`podsync.itunesdb_shared.mhbd_defs`, read side):

| Field | Off | Type | Min hdr | Default | Read as |
|---|---|---|---|---|---|
| `compressed` | 0x0C | u32 | — | 1 | int (2 = iTunesCDB) |
| `version` | 0x10 | u32 | — | 0 | int |
| `child_count` | 0x14 | u32 | — | 0 | int |
| `db_id` | 0x18 | u64 | — | 0 | int |
| `platform` | 0x20 | u16 | — | 2 | int (1=Mac, 2=Windows) |
| `unk0x22` | 0x22 | u16 | — | 0 | int |
| `db_id_2` | 0x24 | u64 | — | 0 | int |
| `unk0x2c` | 0x2C | u32 | — | 0 | int |
| `hashing_scheme` | 0x30 | u16 | — | 0 | int |
| `unk0x32` | 0x32 | raw 20 | — | 20×00 | bytes |
| `language` | 0x46 | raw 2 | — | `b"en"` | bytes (read always; default only pre-write) |
| `db_persistent_id` | 0x48 | u64 | — | 0 | int |
| `unk0x50` | 0x50 | u32 | — | 1 | int |
| `unk0x54` | 0x54 | u32 | — | 15 | int |
| `hash58` | 0x58 | raw 20 | — | 20×00 | bytes |
| `timezone_offset` | 0x6C | i32 | — | 0 | int (seconds east of UTC; consumed by `ipod_library`) |
| `hash_type_indicator` | 0x70 | u16 | — | 0 | int |
| `hash72` | 0x72 | raw 46 | — | 46×00 | bytes |
| `audio_language` | 0xA0 | u16 | 0xA2 | 0 | int |
| `subtitle_language` | 0xA2 | u16 | 0xA4 | 0 | int |
| `unk0xa4` | 0xA4 | u16 | 0xA6 | 0 | int |
| `unk0xa6` | 0xA6 | u16 | 0xA8 | 0 | int |
| `cdb_flag` | 0xA8 | u16 | 0xAA | 0 | int |
| `hashab` | 0xAB | raw 57 | 0xE4 | 57×00 | bytes |

Plus `children` added by the parser. Constants for chapter 04:
`MHBD_HEADER_SIZE = 244`; named hash offsets `0x18` (db id), `0x30`
(hashing scheme), `0x32`, `0x58`, `0x72`, `0xAB`.

### 6.2 `mhsd_parser.parse_dataset` — dataset header

| Field | Off | Type | Min hdr | Notes |
|---|---|---|---|---|
| `dataset_type` | 0x0C | u32 | — | required (write-side); 0 if absent |
| `MHSD_HEADER_SIZE` | — | — | — | 96 (constant) |

Behavior by `dataset_type`:

* **`dataset_type == 9` (Genius CUID)** — no child parse:
  * `raw_payload = bytes(data[offset+header_length : offset+chunk_length])`
  * `genius_cuid = raw_payload.decode("ascii")`, or on
    `UnicodeDecodeError` `raw_payload.hex()`
  * `children = []`
  * `next_offset = offset + chunk_length`, `_body_end = offset + header_length`
* **all other types** — `children, child_end = parse_children(data, offset + header_length, 1)`
  (**exactly one** child regardless of header contents), `_body_end = child_end`.

Dataset-type → child list tag (from
`podsync.itunesdb_shared.constants.chunk_type_map`, authoritative):

| Type | Key / tag | Child content |
|---|---|---|
| 1 | `mhlt` | track list (`mhit` children) |
| 2 | `mhlp` | playlist list (`mhyp` children) |
| 3 | `mhlp_podcast` | podcast-aware playlist list (must sit between types 1 and 2 on disk) |
| 4 | `mhla` | album list (`mhia` children) |
| 5 | `mhlp_smart` | smart/category playlist list |
| 6 | `mhsd_type_6` | empty `mhlt` stub |
| 7 | `mhsd_type_7` | reserved, rarely seen |
| 8 | `mhsd_type_8` | artist list (`mhli` with `mhii` children, MHOD type 300) |
| 9 | `mhsd_type_9` | Genius CUID payload (no list child) |
| 10 | `mhsd_type_10` | empty `mhlt` stub |

### 6.3 `mhit_parser.parse_track_item` — track item

`child_count` = field 0x0C; third generic field = total byte length.

MHIT fields (`podsync.itunesdb_shared.mhit_defs`, read side; "Read as"
notes only where non-obvious):

| Field | Off | Type | Min hdr | Default | Read as |
|---|---|---|---|---|---|
| `child_count` | 0x0C | u32 | — | 0 | int |
| `track_id` | 0x10 | u32 | — | 0 | int (required on write) |
| `visible` | 0x14 | u32 | — | 1 | int |
| `filetype` | 0x18 | u32 | — | 0 | int (→ ASCII in `ipod_library`) |
| `vbr_flag` | 0x1C | u8 | — | 0 | int |
| `mp3_flag` | 0x1D | u8 | — | 0 | int |
| `compilation_flag` | 0x1E | u8 | — | 0 | int |
| `rating` | 0x1F | u8 | — | 0 | int 0–100 |
| `last_modified` | 0x20 | u32 | — | 0 | Mac → Unix |
| `size` | 0x24 | u32 | — | 0 | int bytes |
| `length` | 0x28 | u32 | — | 0 | int ms |
| `track_number` | 0x2C | u32 | — | 0 | int |
| `total_tracks` | 0x30 | u32 | — | 0 | int |
| `year` | 0x34 | u32 | — | 0 | int |
| `bitrate` | 0x38 | u32 | — | 0 | int |
| `sample_rate_1` | 0x3C | u32 | — | 0 | `raw >> 16` Hz |
| `volume` | 0x40 | i32 | — | 0 | int −255..255 |
| `start_time` | 0x44 | u32 | — | 0 | int ms |
| `stop_time` | 0x48 | u32 | — | 0 | int ms |
| `sound_check` | 0x4C | u32 | — | 0 | int |
| `play_count_1` | 0x50 | u32 | — | 0 | int (cumulative) |
| `play_count_2` | 0x54 | u32 | — | 0 | int (pending scrobbles) |
| `last_played` | 0x58 | u32 | — | 0 | Mac → Unix |
| `disc_number` | 0x5C | u32 | — | 0 | int |
| `total_discs` | 0x60 | u32 | — | 0 | int |
| `user_id` | 0x64 | u32 | — | 0 | int |
| `date_added` | 0x68 | u32 | — | 0 | Mac → Unix |
| `bookmark_time` | 0x6C | u32 | — | 0 | int ms |
| `db_track_id` | 0x70 | u64 | — | 0 | int (persistent id) |
| `checked_flag` | 0x78 | u8 | — | 0 | int |
| `app_rating` | 0x79 | u8 | — | 0 | int (rating backup target, §10) |
| `bpm` | 0x7A | u16 | — | 0 | int |
| `artwork_count` | 0x7C | u16 | — | 0 | int |
| `audio_format_flag` | 0x7E | u16 | — | 0xFFFF | int |
| `artwork_size` | 0x80 | u32 | — | 0 | int bytes |
| `unk0x84` | 0x84 | u32 | — | 0 | int |
| `sample_rate_2` | 0x88 | f32 | — | 0.0 | float |
| `date_released` | 0x8C | u32 | — | 0 | Mac → Unix |
| `mpeg_audio_type` | 0x90 | u16 | — | 0 | int |
| `explicit_flag` | 0x92 | u8 | — | 0 | 0/1/2 (§constants) |
| `purchased_aac_flag` | 0x93 | u8 | — | 0 | int |
| `unk0x94` | 0x94 | u32 | — | 0 | int |
| `genius_category_id` | 0x98 | u32 | — | 0 | int |
| `skip_count` | 0x9C | u32 | 0xA0 | 0 | int |
| `last_skipped` | 0xA0 | u32 | 0xA4 | 0 | Mac → Unix |
| `has_artwork` | 0xA4 | u8 | 0xA5 | 0 | int |
| `skip_when_shuffling` | 0xA5 | u8 | 0xA6 | 0 | int |
| `remember_position` | 0xA6 | u8 | 0xA7 | 0 | int |
| `use_podcast_now_playing_flag` | 0xA7 | u8 | 0xA8 | 0 | int |
| `db_track_id_2` | 0xA8 | u64 | 0xB0 | 0 | int |
| `lyrics_flag` | 0xB0 | u8 | 0xB1 | 0 | int |
| `movie_flag` | 0xB1 | u8 | 0xB2 | 0 | int |
| `not_played_flag` | 0xB2 | u8 | 0xB3 | 0 | int |
| `unk0xB3` | 0xB3 | u8 | 0xB4 | 0 | int |
| `unk0xB4` | 0xB4 | u32 | 0xB8 | 0 | int |
| `pregap` | 0xB8 | u32 | 0xBC | 0 | int |
| `sample_count` | 0xBC | u64 | 0xC4 | 0 | int |
| `unk0xC4` | 0xC4 | u32 | 0xC8 | 0 | int |
| `postgap` | 0xC8 | u32 | 0xCC | 0 | int |
| `encoder` | 0xCC | u32 | 0xD0 | 0 | int |
| `media_type` | 0xD0 | u32 | 0xD4 | 1 | int bitmask (`MEDIA_TYPE_*`) |
| `season_number` | 0xD4 | u32 | 0xD8 | 0 | int |
| `episode_number` | 0xD8 | u32 | 0xDC | 0 | int |
| `date_added_to_itunes` | 0xDC | u32 | 0xE0 | 0 | Mac → Unix |
| `store_track_id` | 0xE0 | u32 | 0xE4 | 0 | int |
| `store_encoder_version` | 0xE4 | u32 | 0xE8 | 0 | int |
| `store_artist_id` | 0xE8 | u32 | 0xEC | 0 | int |
| `unk0xEC` | 0xEC | u32 | 0xF0 | 0 | int |
| `store_album_id` | 0xF0 | u32 | 0xF4 | 0 | int |
| `store_content_flag` | 0xF4 | u32 | 0xF8 | 0 | int |
| `gapless_audio_payload_size` | 0xF8 | u32 | 0xFC | 0 | int |
| `unk0xFC` | 0xFC | u32 | 0x100 | 0 | int |
| `gapless_track_flag` | 0x100 | u16 | 0x102 | 0 | int |
| `gapless_album_flag` | 0x102 | u16 | 0x104 | 0 | int |
| `hash_0x104` | 0x104 | raw 20 | 0x118 | 20×00 | bytes |
| `unk0x118` | 0x118 | u32 | 0x11C | 0 | int |
| `unk0x11C` | 0x11C | u32 | 0x120 | 0 | int |
| `album_id` | 0x120 | u32 | 0x124 | 0 | int |
| `db_id_2_ref` | 0x124 | u64 | 0x12C | 0 | int |
| `size_2` | 0x12C | u32 | 0x130 | 0 | int |
| `unk0x130` | 0x130 | u32 | 0x134 | 0 | int |
| `sort_mhod_indicators` | 0x134 | raw 8 | 0x13C | 8×00 | bytes (→ list[int] in `ipod_library`) |
| `unk0x154` | 0x154 | u32 | 0x158 | 0 | int |
| `artwork_id_ref` | 0x160 | u32 | 0x164 | 0 | int (hydrated by §11) |
| `unk0x164` | 0x164 | u32 | 0x168 | 0 | int |
| `unk0x168` | 0x168 | u32 | 0x16C | 1 | int |
| `unk0x173` | 0x173 | u8 | 0x174 | 0 | int |
| `movie_flag_2` | 0x194 | u8 | 0x195 | 0 | int |
| `purchased_aac_flag_2` | 0x195 | u8 | 0x196 | 0 | int |
| `unk0x197` | 0x197 | u8 | 0x198 | 0 | int |
| `unk0x1A0` | 0x1A0 | u32 | 0x1A4 | 0 | int |
| `store_track_id_2` | 0x1B0 | u64 | 0x1B8 | 0 | int |
| `store_encoder_version_2` | 0x1B8 | u64 | 0x1C0 | 0 | int |
| `store_artist_id_2` | 0x1C0 | u64 | 0x1C8 | 0 | int |
| `unk0xEC_2` | 0x1C8 | u64 | 0x1D0 | 0 | int |
| `store_album_id_2` | 0x1D0 | u64 | 0x1D8 | 0 | int |
| `store_content_flag_2` | 0x1D8 | u64 | 0x1E0 | 0 | int |
| `artist_id_ref` | 0x1E0 | u32 | 0x1E4 | 0 | int |
| `unk0x1EC` | 0x1EC | u32 | 0x1F0 | 0 | int |
| `composer_id` | 0x1F4 | u32 | 0x1F8 | 0 | int |
| `unk0x1F8` | 0x1F8 | u32 | 0x1FC | 0 | int |
| `unk0x20C` | 0x20C | u32 | 0x210 | 0 | int |
| `unk0x229` | 0x229 | u8 | 0x22A | 0 | int |
| `unk0x22B` | 0x22B | u8 | 0x22C | 0 | int |

Gaps not covered by any field: 0x13C..0x153, 0x158..0x15F (zero in
observed databases) — preserved only through `_raw_chunk.raw_header`.
Writer-side constant: `MHIT_HEADER_SIZE = 0x270`;
`mhit_header_size_for_version(db_version)` → `0x9C` (≤0x12),
`0x148` (≤0x19), `0x1F8` (≤0x2D), else `0x270` (chapter 04).

### 6.4 `mhyp_parser.parse_playlist` — playlist

After `read_fields("mhyp", header_length)` the parser **derives four
extra keys** (aliases kept for compatibility):

| Derived key | Value |
|---|---|
| `podcast_flag` | `= playlist_kind_flags` (raw word copy, not a boolean) |
| `unk0x30_playlist_ref` | `= parent_folder_playlist_id` |
| `is_podcast` | `bool(playlist_kind_flags & 0x0001)` |
| `is_folder` | `bool(playlist_kind_flags & 0x0100)` |

Then, sequentially from `offset + header_length`:
`mhod_children = parse_children(..., mhod_child_count)` followed by
`mhip_children = parse_children(..., mhip_child_count)`;
`_body_end` = end of MHIP children.

MHYP fields:

| Field | Off | Type | Min hdr | Default | Read as |
|---|---|---|---|---|---|
| `mhod_child_count` | 0x0C | u32 | — | 0 | int |
| `mhip_child_count` | 0x10 | u32 | — | 0 | int |
| `master_flag` | 0x14 | u8 | — | 0 | int |
| `flag1` | 0x15 | u8 | — | 0 | int |
| `flag2` | 0x16 | u8 | — | 0 | int |
| `flag3` | 0x17 | u8 | — | 0 | int |
| `timestamp` | 0x18 | u32 | — | 0 | Mac → Unix |
| `playlist_id` | 0x1C | u64 | — | 0 | int |
| `unk0x24` | 0x24 | u32 | — | 0 | int |
| `string_mhod_child_count` | 0x28 | u16 | — | 0 | int |
| `playlist_kind_flags` | 0x2A | u16 | — | 0 | raw word (0x0001 podcast, 0x0100 folder) |
| `sort_order` | 0x2C | u32 | — | 0 | int (`PLAYLIST_SORT_ORDER_MAP`) |
| `parent_folder_playlist_id` | 0x30 | u64 | — | 0 | int |
| `unk0x38` | 0x38 | u32 | — | 0 | int |
| `db_id_2` | 0x3C | u64 | 0x44 | 0 | int |
| `playlist_id_2` | 0x44 | u64 | 0x4C | 0 | int |
| `unk0x4C` | 0x4C | u32 | 0x50 | 0 | int |
| `mhsd5_type` | 0x50 | u16 | 0x52 | 0 | int (Movies=2, TV=3, Music=4, Books=5, Rentals=7) |
| `phase_game_flag` | 0x52 | u16 | 0x54 | 0 | int (forensics status `observed`) |
| `mhsd5_special_flag` | 0x54 | u32 | 0x58 | 0 | int |
| `timestamp_2` | 0x58 | u32 | 0x5C | 0 | Mac → Unix |

`MHYP_HEADER_SIZE = 184`. Kind helpers live in
`podsync.itunesdb_shared.playlist_kinds`:

| Function | Behavior |
|---|---|
| `playlist_kind_flags(value)` | Mapping → `int(value.get("playlist_kind_flags", value.get("podcast_flag", 0))) & 0xFFFF`, then OR `0x0001` when `value.get("is_podcast") is True`, OR `0x0100` when `value.get("is_folder") is True`; non-mapping → `int(value or 0) & 0xFFFF`; bad types → 0 |
| `is_podcast_playlist(value)` | `bool(flags & 0x0001)` |
| `is_playlist_folder(value)` | `bool(flags & 0x0100)` |

### 6.5 `mhip_parser.parse_playlist_item`

`child_count` = field 0x0C; children are MHODs (type 100 position).

| Field | Off | Type | Min hdr | Default | Read as |
|---|---|---|---|---|---|
| `child_count` | 0x0C | u32 | — | 0 | int |
| `podcast_group_flag` | 0x10 | u16 | — | 0 | int (0x0100 = group header) |
| `unk0x12` | 0x12 | u16 | — | 0 | int |
| `group_id` | 0x14 | u32 | — | 0 | int |
| `track_id` | 0x18 | u32 | — | 0 | int (required on write; 0 on group headers) |
| `timestamp` | 0x1C | u32 | — | 0 | Mac → Unix |
| `group_id_ref` | 0x20 | u32 | — | 0 | int |
| `unk0x24_group_persistent_id` | 0x24 | u64 | — | 0 | int |
| `track_persistent_id` | 0x2C | u64 | 0x34 | 0 | int |
| `mhip_persistent_id` | 0x3C | u64 | 0x44 | 0 | int |

`MHIP_HEADER_SIZE = 76`.

### 6.6 `mhia_parser.parse_album_item`

| Field | Off | Type | Min hdr | Default |
|---|---|---|---|---|
| `child_count` | 0x0C | u32 | — | 0 |
| `album_id` | 0x10 | u32 | — | 0 (required on write) |
| `sql_id` | 0x14 | u64 | — | 0 |
| `platform_flag` | 0x1C | u16 | — | 2 |
| `album_compilation_flag` | 0x1E | u16 | — | 0 |
| `album_track_db_id` | 0x20 | u64 | 0x28 | 0 |
| `album_rating` | 0x28 | u8 | — | 0 |
| `unk0x29_rating_flag` | 0x29 | u8 | — | 0 |
| `season_number` | 0x2C | u32 | — | 0 |

The last three fields have no header-length guard: they are always read
(a header shorter than 0x30 therefore reads whatever bytes follow it, or
fails with `struct.error` at the buffer end, §15). A 0x58-byte header reads
the full dict:

```python
{"child_count", "album_id", "sql_id", "platform_flag",
 "album_compilation_flag", "album_track_db_id", "album_rating",
 "unk0x29_rating_flag", "season_number", "children"}
```

(exact equality asserted by the forensics MHIA test — no other keys).
`MHIA_HEADER_SIZE = 88`.

### 6.7 `mhii_parser.parse_artist_item`

In iTunesDB context this is an **artist record** (it shares the
`mhii` tag with ArtworkDB image items, which is a different package).

| Field | Off | Type | Min hdr | Default |
|---|---|---|---|---|
| `child_count` | 0x0C | u32 | — | 0 |
| `artist_id` | 0x10 | u32 | — | 0 (required on write) |
| `sql_id` | 0x14 | u64 | — | 0 |
| `platform_flag` | 0x1C | u32 | — | 2 |

`MHII_HEADER_SIZE = 80`. Children = MHOD type 300 (`Artist`) strings.

## 7. `podsync.itunesdb_parser.mhod_parser`

### 7.1 Entry point and header

```python
parse_mhod(data, offset, header_length, chunk_length) -> ParseResult
```

Reads `MHOD_FIELDS` **without** a header-length guard (fields: `mhod_type`
u32 @0x0C required, `unk0x10` u32 @0x10, `unk0x14` u32 @0x14;
`MHOD_HEADER_SIZE = 24`). Body:
`body_offset = offset + header_length`,
`body_length = chunk_length - header_length`
(negative/zero → treated as empty by every decoder below).
`next_offset = offset + chunk_length`; `_body_end = offset + header_length`.

### 7.2 Type families (authoritative sets from `mhod_defs`)

| Set | Types | Result key | Decoder |
|---|---|---|---|
| `STRING_MHOD_TYPES` | 1–14, 18–31, 33–44, 200–204, 300 | `string` (+`unk_0x20`, `unk_0x24`) | §7.3 |
| `PODCAST_URL_MHOD_TYPES` | 15, 16 | `string` | §7.4 |
| `CHAPTER_DATA_MHOD_TYPES` | 17 | `data` (dict) | §7.5 |
| `BINARY_BLOB_MHOD_TYPES` | 32 | `string` (hex) | §7.6 |
| `NON_STRING_MHOD_TYPES` | 50, 51, 52, 53, 55, 100, 102 | `data` (dict) | §7.7–7.13 |
| anything else | e.g. 45–49, 54, 99, 101, 103–199 | `string = ""` | stub, body unparsed |

(Type 11 is inside `STRING_MHOD_TYPES` and is decoded as a string, but it
has no `mhod_type_map` name, so extraction ignores it.)

`mhod_type_map` (names — used by extraction keys and forensics
captions; verbatim from `podsync.itunesdb_shared.constants`):

| Id | Name | Id | Name | Id | Name |
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
| 32 | Unknown for Video Track | 33–36 | Unknown (33…36) | 37 | Content Provider |
| 38 | Unknown (38) | 39 | Copyright | 40 | Unknown (40) |
| 41 | Unknown (41) | 42 | Encoding Quality Descriptor | 43 | Purchase Account |
| 44 | Purchaser Name | 50 | Smart Playlist Data | 51 | Smart Playlist Rules |
| 52 | Library Playlist Index | 53 | Library Playlist Jump Table | 55 | Playlist Property Plist |
| 100 | Column Size or Playlist Order | 102 | Playlist Settings (binary) | 200 | Album (Used by Album Item) |
| 201 | Artist (Used by Album Item) | 202 | Sort Artist (Used by Album Item) | 203 | Podcast URL (Used by Album Item) |
| 204 | Show (Used by Album Item) | 300 | Artist (Used by Artist Item) | | |

(Id 11, 44<id<50 except map entries, 54, 99, 101, 205+ etc. are not
in the map → extraction skips them even when parsed.)

### 7.3 Standard string MHODs

16-byte sub-header at absolute 0x18 (i.e. body start), string payload
at absolute 0x28:

| Off | Size | Key | Meaning |
|---|---|---|---|
| 0x18 | u32 LE | (not stored) | encoding: 2 → UTF-8; 0/1/other → UTF-16LE |
| 0x1C | u32 LE | (not stored) | `string_length` in **bytes** |
| 0x20 | u32 LE | `unk_0x20` | opaque (observed constant) |
| 0x24 | u32 LE | `unk_0x24` | opaque (observed constant) |
| 0x28 | `string_length` B | `string` | decoded text |

Constants: `MHOD_STRING_SUBHEADER_OFFSET = 0x18`,
`MHOD_STRING_SUBHEADER_SIZE = 16`, `MHOD_STRING_DATA_OFFSET = 0x28`.

Exact behavior:

* `string = data[0x28 + offset : 0x28 + offset + string_length]`
  decoded with `errors="replace"`.
* The slice is clamped **only by the end of the whole file buffer**,
  not by the chunk's declared length: an oversized `string_length`
  reads into following bytes (a known leniency; round-trip safety is
  the writer's job, chapter 04).
* No NUL stripping and no even-length enforcement — an odd byte count
  under UTF-16LE yields a replacement character at the end.
* Zero length → `string == ""`.

### 7.4 Podcast URL MHODs (15, 16)

Body straight after the 24-byte header, **no sub-header**:
`string = body.decode("utf-8", errors="replace").rstrip("\x00")`;
`body_length <= 0` → `string == ""`.

### 7.5 Chapter data (17)

12-byte little-endian preamble, then a **big-endian atom tree**
(`sean` → `chap`×N → `name`, then `hedr`).

Preamble (body-relative):

| Off | Key |
|---|---|
| +0x00 | `unk024` (u32 LE) |
| +0x04 | `unk028` (u32 LE) |
| +0x08 | `unk032` (u32 LE) |

Atom layouts (all u32 BE unless noted):

| Atom | Off | Field |
|---|---|---|
| `sean` (20+ bytes) | +0x00 | total_size; +0x04 `"sean"`; +0x08 unknown (=1); +0x0C child_count (= chapters + 1 for `hedr`); +0x10 unknown (=0) |
| `chap` (20+ bytes) | +0x00 | total_size; +0x04 `"chap"`; +0x08 startpos (ms); +0x0C child_count (=1); +0x10 unknown (=0) |
| `name` | +0x00 | total_size; +0x04 `"name"`; +0x08 unknown (=1); +0x0C/+0x10 unknown (=0); **+0x14 u16 BE string_length (code units)**; +0x16 UTF-16BE title |
| `hedr` | 28 bytes | +0x00 size=28; +0x04 `"hedr"`; +0x08 (=1); +0x0C child_count=0; +0x10/+0x14 (=0); +0x18 (=1) |

Constants: `CHAPTER_PREAMBLE_SIZE = 12`, `SEAN_ATOM/CHAP_ATOM/
NAME_ATOM/HEDR_ATOM = b"sean"/b"chap"/b"name"/b"hedr"`,
`HEDR_SIZE = 28`.

Algorithm and edge behavior (result key `data`):

1. `body_length < 12` → WARNING `MHOD17 (chapter data) too short for preamble: %d bytes`, result `{"chapters": []}` (**no unk keys**).
2. Read the three preamble u32 LE.
3. `< 20` bytes left → `{"chapters": []}` (no warning).
4. `sean_size` (u32 BE) `< 20` or extends past body end → WARNING `Chapter data: invalid 'sean' atom size: %d` → `{"chapters": []}`.
5. Magic ≠ `b"sean"` → WARNING `Chapter data: expected 'sean' atom, got %r` → `{"chapters": []}`.
6. `num_chapters = max(0, sean_child_count - 1)`; loop bounded by that
   count. Each iteration: needs 20 bytes else break; magic at +4 ≠
   `b"chap"` → **break** (stop parsing, keep chapters so far);
   read `chap_size`, `startpos`, child count; scan that many child
   atoms: needs 22 bytes else break; child magic `b"name"` →
   `str_len` u16 BE at +20, title from +22 (`str_len * 2` bytes) only
   when it fits before the body end, decoded UTF-16BE; otherwise keep
   previous title (starts as `""`); advance by the child's own
   `child_size`.
7. Append `{"startpos": int, "title": str}` per chapter.
8. If the trailing atom is `hedr`, skip `hedr_size` (result is not
   affected).

Full dict keys on success: `unk024`, `unk028`, `unk032`, `chapters`.
`podsync.itunesdb_shared.extraction.extract_track_extras` surfaces this
as `{"chapter_data": <dict>}` (also when `chapters` is empty).

### 7.6 Binary blob (32)

`string = body.hex()` — the raw bytes are preserved as a lowercase hex
string for JSON round-tripping. No structure is assigned (observed
84-byte AVC/H.264 descriptors remain uninterpreted).

### 7.7 MHOD type 50 — SPLPref (`data` dict)

| Body off | Size | Key |
|---|---|---|
| +0x00 | u8 | `live_update` |
| +0x01 | u8 | `check_rules` |
| +0x02 | u8 | `check_limits` |
| +0x03 | u8 | `limit_type` |
| +0x04 | u8 | `limit_sort` (raw byte, reverse flag NOT folded in) |
| +0x05 | 3 | padding (annotated `status: "padding"` in forensics) |
| +0x08 | u32 LE | `limit_value` |
| +0x0C | u8 | `match_checked_only` (only when `body_length ≥ 13`) |
| +0x0D | u8 | `reverse_sort` (only when `body_length ≥ 14`) |

`body_length < 12` → WARNING `MHOD50 (SPLPref) body too short: %d bytes`
→ `{}`. Base keys (6) always present when `body_length ≥ 12`, in order:
`live_update, check_rules, check_limits, limit_type, limit_sort,
limit_value`. `SPLPREF_BODY_SIZE = 72` (writer emits 96-byte chunks
total). Value vocabularies (`SPL_LIMIT_TYPE_MAP`, `SPL_LIMIT_SORT_MAP`)
live in `podsync.itunesdb_shared.mhod_defs` / chapter 03; the parser
stores raw ints only.

### 7.8 MHOD type 51 — SLst smart-playlist rules (**big-endian**)

Entry from `parse_mhod`, but also reachable directly:
`_parse_mhod51(data, body_offset, body_length)` (module-level helper,
importable by tests) — `body_offset`/`body_length` are **absolute
within `data`** (callers pass `MHOD_HEADER_SIZE` and
`len(data) - MHOD_HEADER_SIZE` for a bare MHOD blob starting at 0);
it runs the guards below and delegates to `_parse_slst(data,
body_offset, body_length)`, returning the same dict `parse_mhod` puts
under `data`.

Everything multi-byte inside the body is big-endian; this is the only
BE section besides chapter atoms.

SLst container (136 bytes, `SLST_HEADER_SIZE = 136`):

| Body off | Type | Key |
|---|---|---|
| +0x00 | 4 B | magic `b"SLst"` (not stored as a key) |
| +0x04 | u32 BE | `unk004` (observed/default `0x00010001`) |
| +0x08 | u32 BE | `rule_count` |
| +0x0C | u32 BE | `conjunction` (0=AND, 1=OR) |
| +0x10 | 120 B | padding |

Guards: `body_length < 136` → WARNING `MHOD51 (SPLRules) body too short: %d bytes` → `{}`;
magic ≠ `b"SLst"` → WARNING `MHOD51: expected SLst magic, got %r` → `{}`.

Rules are read from `body + 136`, at most `rule_count` of them, stopping
early when fewer than 56 bytes remain before `body_end`; bytes after the
last counted rule are ignored. Each rule consumes `56 + data_length` bytes
(`SPL_RULE_HEADER_SIZE = 56`). Result: `{"unk004", "rule_count",
"conjunction", "rules": [...]}`.

Rule header (56 bytes, all BE):

| Rule off | Type | Key |
|---|---|---|
| +0x00 | u32 BE | `field_id` |
| +0x04 | u32 BE | `action_id` |
| +0x08 | u32 BE | `group_marker` (leaf: 0) |
| +0x0C | 40 B | opaque tail (`SPL_GROUP_HEADER_BYTES_SIZE = 40`) |
| +0x34 | u32 BE | `data_length` |
| +0x38 | `data_length` B | payload |

`available_length = min(data_length, container_end − payload_start)`
(clamped to the buffer too).

**Group rule** — recognized only when *all* hold:
`field_id == 0`, `action_id == 1`, `group_marker == 0x01000000`
(`SPL_GROUP_MARKER`), `data_length ≥ 136`, `available_length ==
data_length`, and payload starts with `b"SLst"`. Then the rule dict is
exactly `{"field_id", "action_id", "data_length", "header_bytes",
"group_marker", "group"}` where `header_bytes` = the 40 opaque bytes
and `group` = recursively parsed SLst (`{"unk004", "rule_count",
"conjunction", "rules"}`; a nested truncated container yields `{}` for
`group`… see below). Missing any discriminator → falls through to
leaf parsing (test: no `group` key).

**String leaf** — when `spl_get_field_type(field_id) ==
SPLFT_STRING (1)`, or the type is `SPLFT_UNKNOWN (6)` **and**
(`data_length == 0` or `action_id & 0x01000000` set):
`string_value` = payload decoded UTF-16BE (`""` when
`data_length == 0`); additionally `inferred_field_type = "string"`
for unknown field ids. Result keys: `field_id`, `action_id`,
`data_length`, `string_value`[, `inferred_field_type`].

**Numeric leaf** — all other field types (`SPLFT_INT/DATE/BOOLEAN/
PLAYLIST/BINARY_AND`):

* `available_length < SPL_RULE_DATA_SIZE (0x44 = 68)` → opaque
  preservation: keys `field_id, action_id, data_length, header_bytes,
  group_marker, raw_data` where `raw_data = bytes(payload[:available_length])`
  (a truncated nested `SLst` payload lands here, e.g.
  `raw_data == b"SLst" + <16 bytes>`), total consumed still
  `56 + data_length`.
* otherwise payload keys (offsets from payload start, all BE):

| Off | Type | Key |
|---|---|---|
| +0x00 | u64 BE | `from_value` |
| +0x08 | i64 BE | `from_date` (signed) |
| +0x10 | u64 BE | `from_units` |
| +0x18 | u64 BE | `to_value` |
| +0x20 | i64 BE | `to_date` (signed) |
| +0x28 | u64 BE | `to_units` |
| +0x30 | u32 BE | `unk052` |
| +0x34 | u32 BE | `unk056` |
| +0x38 | u32 BE | `unk060` |
| +0x3C | u32 BE | `unk064` |
| +0x40 | u32 BE | `unk068` |

Field-type lookup (`SPL_FIELD_TYPE_MAP` class lists — parser-observable
partition; full map + label tables in `podsync.itunesdb_shared.mhod_defs`,
chapter 03):

| Class | Field ids |
|---|---|
| `SPLFT_STRING (1)` | 0x02, 0x03, 0x04, 0x08, 0x09, 0x0E, 0x12, 0x27, 0x36, 0x37, 0x3E, 0x47, 0x4E, 0x4F, 0x50, 0x51, 0x52, 0x53, 0x59, 0x9F, 0xA0 |
| `SPLFT_INT (2)` | 0x05, 0x06, 0x07, 0x0B, 0x0C, 0x0D, 0x16, 0x18, 0x19, 0x23, 0x39, 0x3C, 0x3F, 0x44, 0x5A, 0x86, 0x9A, 0x9C, 0xA1 |
| `SPLFT_DATE (4)` | 0x0A, 0x10, 0x17, 0x45 |
| `SPLFT_BOOLEAN (3)` | 0x1D, 0x1F, 0x25, 0x29 |
| `SPLFT_PLAYLIST (5)` | 0x28 |
| `SPLFT_BINARY_AND (7)` | 0x85 |
| `SPLFT_UNKNOWN (6)` | anything else → `spl_get_field_type` default |

Relative-date helpers used by writers/evaluators (not the parser):
`SPL_DATE_RELATIVE_ACTION_IDS = {0x00000200, 0x02000200}`,
`SPL_DATE_IDENTIFIER = 0x2DAE2DAE2DAE2DAE`.

### 7.9 MHOD type 52 — library playlist sorted index

`body_length < 8` → WARNING `MHOD52 (sorted index) body too short: %d bytes` → `{}`.

| Body off | Type | Key |
|---|---|---|
| +0x00 | u32 LE | `sort_type` |
| +0x04 | u32 LE | `count` |
| +0x08 | 40 B | padding (`MHOD52_BODY_HEADER_SIZE = 48`) |
| +0x30 | count × u32 LE | `indices` (list of ints) |

Each index is read only when `pos + 4 ≤ body_end` (truncated tails
produce a shorter list, never an error).

### 7.10 MHOD type 53 — jump table

`body_length < 8` → WARNING `MHOD53 (jump table) body too short: %d bytes` → `{}`.

| Body off | Type | Key |
|---|---|---|
| +0x00 | u32 LE | `sort_type` |
| +0x04 | u32 LE | `count` |
| +0x08 | 8 B | padding (`MHOD53_BODY_HEADER_SIZE = 16`) |
| +0x10 | count × 12 B | `entries` |

Entry (`MHOD53_ENTRY_SIZE = 12`): `letter_code` u16 LE at +0, pad 2,
`start` u32 LE at +4, `count` u32 LE at +8 → dict
`{"letter_code", "start", "count"}`; read only when the full 12 bytes
fit.

### 7.11 MHOD type 55 — playlist property plist

Body is passed verbatim to
`podsync.itunesdb_shared.playlist_properties.parse_playlist_property_mhod55`,
which returns:

| Key | Value |
|---|---|
| `raw_body` | `bytes` (always bytes; `b""` never occurs here since the raw slice is bytes) |
| `plist` | `dict` — result of `plistlib.loads` when it yields a dict, else `{}` |
| `description` | added only when `plist["description"]` is a non-empty… (any `str`) value |

Invalid/non-dict plists → `{"raw_body": <bytes>, "plist": {}}` with no
`description` key. Lifecycle helpers
(`playlist_property_from_row`, `playlist_description_from_row`,
`normalize_playlist_description`, `playlist_description_update_fields`,
`playlist_property_raw_body_for_write`) are chapter 03 material;
constants: `PLAYLIST_PROPERTY_KEY = "playlist_property_plist"`,
`PLAYLIST_DESCRIPTION_KEY = "playlist_description"`,
`PLAYLIST_DESCRIPTION_DUPLICATE_KEY = "Album"`.

### 7.12 MHOD type 100 — position vs. display preferences

Branch on body size (`MHOD100_POSITION_BODY_SIZE = 20`):

* `body_length ≤ 20` (**MHIP context**): `body_length ≥ 4` →
  `{"position": u32 LE at body+0}`; else `{}`. The remainder is
  padding (forensics: `status: "padding"`).
* `body_length > 20` (**MHYP context**, ~624 bytes):
  `{"fields": {…}, "raw_body": bytes}` where `fields` comes from the
  nonzero scan below and `raw_body` is the exact body.

### 7.13 MHOD type 102 — playlist settings

Always `{"fields": {…}, "raw_body": bytes}` (observed bodies: 332
bytes; semantics opaque).

**Nonzero scan** (`_scan_nonzero_fields`) used by both: walk the body;
for each nonzero byte not yet visited, if its 4-byte-aligned u32 is
within the body and nonzero, emit `f"0x{aligned:03X}" → u32 LE` and
mark those 4 bytes visited; otherwise emit `f"0x{i:03X}" → body[i]`
(single byte). Keys are hex-offset strings with 3 uppercase hex digits.

## 8. `podsync.itunesdb_parser.parser`

### 8.1 `decompress_itunescdb(data: bytes | bytearray) -> bytes | bytearray`

Decision sequence:

1. `len(data) < 16` or `data[:4] != b"mhbd"` → return `data` unchanged.
2. `header_length = u32 LE @ 0x04`; `flag = u32 LE @ 0x0C`.
3. `flag != 2` → return `data` unchanged.
4. `zlib.decompress(data[header_length:])`; on `zlib.error` → return
   `data` unchanged ("flag set but not actually compressed").
5. Success → `data[:header_length] + decompressed`, i.e. the original
   header is preserved **byte-for-byte** (stale `total_length` at +0x08
   and the compression flag at +0x0C are intentionally kept; children
   are parsed by `child_count`, so the stale length is harmless) and
   logs DEBUG `iTunesCDB decompressed: %d -> %d payload bytes`
   (payload before, payload after).

### 8.2 `parse_itunesdb(file, *, time_context=None, preserve_raw=False) -> dict`

```python
def parse_itunesdb(
    file: str | os.PathLike[str] | BinaryIO,
    *,
    time_context: DeviceTimeContext | None = None,
    preserve_raw: bool = False,
) -> dict[str, Any]: ...
```

Steps:

1. Input: `str`/`os.PathLike` → `open(file, "rb").read()` (`OSError`
   propagates); object with `.read` → `data = file.read()`; anything
   else → `TypeError(f"file must be a path (str/PathLike) or a file-like object, got {type(file).__name__}")`.
2. `not data` → `CorruptHeaderError(0, "empty file")`.
3. `data = decompress_itunescdb(data)`.
4. `reset_unknown_chunk_summary()`.
5. Inside `preserve_raw_chunks(preserve_raw)`:
   * `time_context is None` → `parse_chunk(data, 0)` directly;
   * else inside `use_device_time_context(time_context)` →
     `parse_chunk(data, 0)` (all Mac-epoch `read_transform`s in the
     tree convert through this context);
   * then `log_unknown_chunk_summary()`.
6. `result = parsed["data"]`; when `preserve_raw` **and** `result` is
   a dict **and** `"_raw_chunk" in parsed` →
   `result["_raw_chunk"] = parsed["_raw_chunk"]` (root metadata
   attached to the mhbd dict).
7. Return `result`.

Notes:

* The root chunk is whatever tag sits at offset 0 — expected `mhbd`;
  a non-`mhbd` root still dispatches by its tag (or the unknown path).
* Parse exceptions are **not** caught; they propagate to the caller.
* `preserve_raw=False` (default) guarantees no `_raw_chunk` key
  anywhere in the tree.

## 9. `podsync.itunesdb_parser.playcounts`

The firmware never edits iTunesDB; it records per-track deltas in
`iPod_Control/iTunes/Play Counts`. After a sync has folded those deltas
into a newly written database the file must be deleted, so the firmware
starts a fresh one (chapter 08 owns the deletion policy).

### 9.1 File layout

Header (16+ bytes, all u32 LE):

| Off | Key |
|---|---|
| 0x00 | magic `b"mhdp"` |
| 0x04 | `header_len` |
| 0x08 | `entry_len` |
| 0x0C | `entry_count` |
| 0x10.. | padding to `header_len` |

Entry fields (present when `entry_len` reaches the given size; 28 B
for the common `entry_len == 0x1C`):

| Entry off | Guard | Field |
|---|---|---|
| +0x00 | always | `play_count` |
| +0x04 | `≥ 8` | `last_played_mac` |
| +0x08 | `≥ 12` | `bookmark_time` |
| +0x0C | `≥ 16` | raw rating (see rule below) |
| +0x10 | `≥ 20` | unk16/podcast — **skipped, never stored** |
| +0x14 | `≥ 24` | `skip_count` |
| +0x18 | `≥ 28` | `last_skipped_mac` |

Rating rule: raw `> 0` → `entry.rating = raw`; raw `== 0` → stays
`-1` ("firmware zeroes untouched entries; 0 must not clear a PC
rating"). `entry_len ≥ 16` is the guard for reading it at all.

Entries align 1:1 **by index** with the `mhlt` track list (never by
track id).

### 9.2 `PlayCountEntry` (`dataclass(slots=True)`)

| Field / member | Type | Default | Notes |
|---|---|---|---|
| `play_count` | int | 0 | |
| `last_played_mac` | int | 0 | device-local Mac time; 0 = not played |
| `bookmark_time` | int | 0 | |
| `rating` | int | **-1** | -1 = no change; 0–100 = new rating |
| `skip_count` | int | 0 | |
| `last_skipped_mac` | int | 0 | 0 = not skipped |
| `has_data` (property) | bool | — | `play_count > 0 or skip_count > 0 or rating >= 0` |
| `last_played_unix` (property) | int | — | uses `current_device_time_context()` |
| `last_skipped_unix` (property) | int | — | ditto |
| `last_played_as_unix(time_context)` | int | — | `time_context.mac_to_unix(...)` |
| `last_skipped_as_unix(time_context)` | int | — | ditto |

### 9.3 `parse_playcounts(path: str | Path) -> list[PlayCountEntry] | None`

| Condition | Result / log |
|---|---|
| file missing | `None`, DEBUG `No Play Counts file at %s` |
| `OSError` on read | `None`, WARNING `Could not read Play Counts file: %s` |
| `len(data) < 16` | `None`, WARNING `Play Counts file too small (%d bytes)` |
| magic ≠ `b"mhdp"` | `None`, WARNING `Play Counts file bad magic: %r (expected b'mhdp')` |
| `len(data) < header_len + entry_len * entry_count` | `None`, WARNING `Play Counts file truncated: %d bytes < expected %d` |
| ok | INFO `Play Counts: header=%d, entry_len=%d, entries=%d`, build list, INFO `Play Counts: %d / %d entries have activity` |

Trailing bytes beyond `expected_size` are ignored (the check is
`<`, not `!=`).

### 9.4 `merge_playcounts(tracks, entries, *, time_context=None) -> None`

Folds deltas **in place**; returns `None`.

1. `time_context = time_context or current_device_time_context()`.
2. `count = min(len(tracks), len(entries))`; on mismatch WARNING
   `Track count (%d) != Play Counts entry count (%d); merging first %d`.
3. For each `i < count`:

| Track key | Rule |
|---|---|
| `recent_playcount` | `= entry.play_count` (session delta) |
| `play_count_1` | `= track.get("play_count_1", 0) + entry.play_count` |
| `play_count_2` | `= track.get("play_count_2", 0) + entry.play_count` (durable pending-scrobble queue; existing value never folded elsewhere) |
| `recent_skipcount` | `= entry.skip_count` |
| `skip_count` | `= track.get("skip_count", 0) + entry.skip_count` |
| `rating` | only when `entry.rating >= 0` **and** differs from `track.get("rating", 0)`: first `track["app_rating"] = old`, then `track["rating"] = entry.rating` |
| `bookmark_time` | overwritten only when `entry.bookmark_time > 0` |
| `last_played` | when `entry.last_played_mac > 0` and `unix > track.get("last_played", 0)` → replace (compares Unix vs Unix — no double conversion) |
| `last_skipped` | same rule via `last_skipped_mac` |

4. For `i ≥ count` (tracks beyond the entries): set
   `recent_playcount = 0` and `recent_skipcount = 0`; all other keys
   untouched (pending-scrobble counts survive).
5. INFO `Merged Play Counts: %d plays, %d skips, %d ratings across %d tracks`
   (counts of tracks with `entry.play_count > 0`,
   `entry.skip_count > 0`, and applied rating changes, respectively).

## 10. `podsync.itunesdb_parser.otg`

Firmware-created playlists live in separate MHPO files next to
iTunesDB: `OTGPlaylistInfo`, `OTGPlaylistInfo_1`, `_2`, …

MHPO layout (all fields through the file's chosen endianness):

| Off | Size | Field |
|---|---|---|
| 0x00 | 4 | magic `b"mhpo"` (LE) or `b"ohpm"` (BE) |
| 0x04 | u32 | `header_len` (usually 0x14) |
| 0x08 | u32 | `entry_len` (usually 4) |
| 0x0C | u32 | `entry_num` |
| 0x10 | 4 | reserved, ignored |
| `header_len` | `entry_num × entry_len` | entries: u32 **0-based index into the mhlt list** |

### 10.1 `load_otg_playlists(itunes_dir: str, track_list: list) -> list[dict]`

Discovery (`_collect_otg_paths`): if `<itunes_dir>/OTGPlaylistInfo` is
missing → `[]` ("only parse if the base exists"); otherwise start with
it, then append `OTGPlaylistInfo_{i}` for `i in 1..19`
(`range(1, 20)`), **stopping at the first missing numbered file**.

Per file, `enumerate(paths, 1)` assigns `pl_num` — numbering counts
discovered *files*, so a failed file still consumes its number:

| Failure | Result / log |
|---|---|
| `OSError` reading | skip file, WARNING `OTG: could not read %s: %s` |
| `len(raw) < 0x14` | skip, WARNING `OTG: %s too short (%d B)` (basename) |
| magic neither `mhpo` nor `ohpm` | skip, WARNING `OTG: %s has unrecognised magic %r — skipping` |
| `header_len < 0x14` | skip, WARNING `OTG: %s header_len %d < 20 — skipping` |
| `entry_len < 4` | skip, WARNING `OTG: %s entry_len %d < 4 — skipping` |
| entry extends past EOF | stop entries, WARNING `OTG: %s entry %d extends past EOF — truncating` |
| index ≥ `len(track_list)` | skip that entry, WARNING `OTG: %s entry %d references track index %d but track list has only %d entries — skipping entry` |
| referenced `track_id` falsy (`0`/missing) | entry silently dropped |
| final `items` empty | **no playlist** (return `None`) |

Endianness: struct format is `"<I"` for `mhpo`, `">I"` for `ohpm` —
applied to header fields **and** entries.

Success returns and logs INFO
`OTG: imported '%s' (%d tracks) from %s`:

| Key | Value |
|---|---|
| `Title` | `f"On-The-Go {pl_num}"` |
| `items` | `[{"track_id": <int>}, ...]` in file order |
| `playlist_id` | `int.from_bytes(hashlib.md5(raw).digest()[:8], "little")` — content-derived, so rescans without a write produce the same id (dedup-friendly) |

`ipod_library` appends successful results to the end of `data["mhlp"]`.

### 10.2 `delete_otg_files(itunes_dir: str) -> None`

Deletes **only** the base `OTGPlaylistInfo` (the firmware removes the
numbered variants itself after it re-reads the database). Missing file
→ silent return; `OSError` → WARNING `Could not delete OTGPlaylistInfo: %s`;
success → INFO `Deleted OTGPlaylistInfo`. This is the one destructive
filesystem call inside the parser package (invoked by the sync layer
after a successful database write — chapter 08).

## 11. `podsync.itunesdb_parser.artwork_links`

Older databases omit usable MHIT `artwork_id_ref` values; the reliable
link is ArtworkDB's MHII `songId` == track `db_track_id`.

| Function | Spec |
|---|---|
| `_artworkdb_path_from_itunesdb(p)` (private) | `Path(p).parent.parent / "Artwork" / "ArtworkDB"` → `<iPod>/iPod_Control/Artwork/ArtworkDB` |
| `_build_song_to_artwork_id(path) -> dict[int,int]` (private) | missing file → `{}`; imports `podsync.artworkdb_parser.parser.parse_artworkdb` **lazily**; **any** `Exception` → DEBUG `Could not parse ArtworkDB for artwork links: %s` → `{}`; else walks `artworkdb.get("mhli", [])`, takes `song_id = int(entry.get("songId") or entry.get("song_id") or 0)` and `img_id = int(entry.get("img_id") or 0)` (bad ints skipped), keeps first mapping per song via `setdefault` when both nonzero |
| `_build_artwork_id_to_song_id(path) -> dict[int,int]` (private) | same parsing and error handling as above, but maps **every** nonzero `img_id` to its nonzero `song_id` (`img_id → song_id`) |
| `_int_or_zero(v)` (private) | `int(v or 0)`, else 0 |
| `hydrate_track_artwork_refs(tracks, itunesdb_path) -> int` | see below |

`hydrate_track_artwork_refs` semantics (returns count of **updated**
tracks):

1. `not tracks` → `0`; empty link map → `0` (ArtworkDB absent/failed —
   graceful no-op, never raises).
2. Per track (non-dicts skipped): `db_track_id = int(db_track_id or db_id or 0)`; `0` → skip; no link for that id → skip.
3. `existing = int(artwork_id_ref or 0)`; `existing == artwork_id` → skip
   (counted as nothing). Also skip when `existing != 0` and
   `_build_artwork_id_to_song_id(...)` (built once per call, only when
   needed) maps `existing` to this same `db_track_id`: the track already
   points at one of **its own** MHIIs, which is valid.
   *Why:* iTunes can leave several MHIIs for one song (observed: 176 of
   1 316 tracks on an iPod Video 5.5G), and links the track to one of them.
   Replacing it with the first MHII would re-point a valid link and log it
   as stale on every read; podsync keeps iTunes' choice.
4. `existing != 0` (stale: missing from ArtworkDB or belonging to another
   song) → INFO `Corrected stale track artwork ref for db_track_id=%d: %d -> %d`.
5. Set `track["artwork_id_ref"] = artwork_id`; if
   `int(track.get("mhii_link") or 0) != artwork_id` → set
   `track["mhii_link"] = artwork_id`; if `not track.get("artwork_count")`
   → `track["artwork_count"] = 1`; `hydrated += 1`.
6. `hydrated > 0` → INFO `Normalized %d track artwork refs from ArtworkDB song links`.

## 12. `podsync.itunesdb_parser.forensics`

A lossless, human-first JSON export: a recursive **byte walk** where
each chunk owns one ordered `bytes` list of spans and nested children.

### 12.1 Formatting helpers (exact)

| Helper | Format |
|---|---|
| offsets | `f"0x{value:04X}"` (4 hex digits, uppercase, zero-padded) |
| hex of bytes | `value.hex(" ")` — lowercase, space-separated (`"19 00"`) |
| `_json_value` | `bytes/bytearray` → `{"hex": <spaced>, "byte_length": n}`; lists/dicts mapped recursively; everything else unchanged |
| field `status` | `"observed"` when the field name starts with `unk`, starts with `mhsd5`, or equals `phase_game_flag`; otherwise `"known"` |

### 12.2 `forensic_json_document(source) -> dict`

1. `source_bytes = Path(source).read_bytes()`.
2. `parsed = parse_itunesdb(source_path, preserve_raw=True)` — any
   parse error propagates.
3. Build:

| Top key | Value |
|---|---|
| `format` | `"podsync-byte-walk/v1"` (fixed literal) |
| `source` | `{"filename": name, "byte_length": len, "sha256": hex digest} |
| `file` | root chunk object (§12.3) |

4. Self-check: `reconstruct_byte_walk(document) != source_bytes` →
   `ValueError("byte-walk reconstruction does not match the source file")`.

`export_forensic_json(source, destination) -> Path` writes
`json.dumps(document, indent=2, ensure_ascii=False) + "\n"` with
`encoding="utf-8"` and returns the output path (parent directories are
**not** created).

### 12.3 Chunk object shape

```json
{"chunk": "mhyp", "caption": "Playlist: Phase Music",
 "file_offset": "0x0C80", "byte_length": 184, "bytes": [ … ]}
```

| Key | Value |
|---|---|
| `chunk` | 4-char tag (first 4 bytes of `raw_header`, ASCII with `errors="replace"`) |
| `caption` | see below |
| `file_offset` | `f"0x{offset:04X}"` |
| `byte_length` | chunk length (`end_offset - offset`) |
| `bytes` | ordered entries |

Caption rules: base name from `identifier_readable_map`
(`mhbd`→"Database", `mhsd`→"Dataset", `mhlt`→"Track List",
`mhlp`→"Playlist or Podcast List", `mhla`→"Album List",
`mhli`→"Artist List", `mhlp_smart`→"Smart Playlist List",
`mhia`→"Album Item", `mhii`→"Artist Item", `mhit`→"Track Item",
`mhyp`→"Playlist", `mhod`→"Data Object", `mhip`→"Playlist Item");
`mhyp` with an MHOD type-1 title → `"Playlist: <title>"`;
`mhod` → `"Data Object: <mhod_type_map name>"` or
`"Data Object: type <n>"` for unmapped ids; a tag missing from the map
uses the raw tag itself as the caption.

Entry kinds (each begins with `at` + `byte_length`):

| Kind | Extra keys |
|---|---|
| leaf span | `hex`, plus `status: "unmapped"` for gaps, or annotation details |
| padding span | `hex`, `status: "padding"`, **no** `field` (fixed zero areas of MHOD 50/51/52/53/100 bodies) |
| annotated span | `field`, `value` (JSON-safe), `status`, optional `encoding` (`ascii`, `u32le`, `u32be`, `u8`, `u16le`, `utf-8`, `utf-16le`, `utf-16be`, `u64be`, …), optional `note` |
| nested chunk | `chunk: {…}` (own object); **no** `hex` |

### 12.4 Annotation generation

**Header** (local offsets): first the three generic fields
(`chunk_type` ascii, `header_length` u32le,
`declared_length_or_child_count` u32le, all `status: "known"`), then
every `FieldDef` of the chunk type with `offset ≥ 12` and
`offset + size ≤ header_length` (`field`, `value`, `status`). Special
case: `mhyp.phase_game_flag` on a playlist titled `"Phase Music"`
adds a `note` ("Observed value 25 (0x0019) occurs in both mirrored
Phase Music playlists; its purpose is not yet proven."). Header bytes
not covered by a field become `status: "unmapped"` gap spans.

**Children**: gathered from `children`, `mhod_children`,
`mhip_children` (or the payload itself when it is a list), deduped by
`_raw_chunk.offset`, sorted by offset, emitted as nested `chunk`
entries at their exact local ranges.

**Leaf bodies** by chunk kind:

| Kind | Annotations |
|---|---|
| `mhod` string types | `string_encoding` (u32le), `string_byte_length` (u32le), `unk_0x20`, `unk_0x24` (both `status: "observed"`, no `encoding`), then `text` over `[16, min(16 + length, body_end))` with `encoding: "utf-8"` if encoding==2 else `"utf-16le"`. No annotations at all when the body is < 16 bytes; only the `text` span is omitted when it would be empty |
| `mhod` podcast URL | whole body as `text` (utf-8) |
| `mhod` 50 | 8 fixed one-byte/u32 fields + padding span for +5..+8 |
| `mhod` 51 | `slst_magic`, `unk004` (u32be), `rule_count`, `conjunction`, padding to 136; per rule: `rule_field_id`, `rule_action_id`, group ⇒ `group_marker` + `group_header_bytes` (else padding +8..+52), `rule_data_length` (u32be); group ⇒ recurse; string ⇒ `rule_text` (utf-16be); numeric ⇒ the 11 payload fields (`encoding: "u64be"` for 8-byte slots else `"u32be"`, status via `_field_status`); short payload ⇒ `rule_data` `status: "partially_decoded"` |
| `mhod` 52/53 | `sort_type`, `count`, padding, then `track_index` per index (u32le) or `jump_table_entry` per 12-byte entry |
| `mhod` 100 position | `position` (u32le) + padding; 100/102 prefs → `observed_nonzero_u32` (u32le, `status: "observed"`) per nonzero scan key |
| `mhod` 17 | one span `chapter_atom_tree`, `status: "partially_decoded"` |
| `mhod` 32/55 | one span `opaque_payload`, `status: "opaque"` |
| other `mhod` | one span `unclassified_mhod_payload`, `status: "opaque"` |
| `mhsd` with `genius_cuid` | whole body as `genius_cuid` (ascii, `status: "known"`) |
| other leaves | `opaque_payload`, `status: "opaque"` when the body is non-empty |

Integrity `ValueError`s raised while building:
`"byte-walk annotation falls outside its chunk"`,
`"byte-walk annotations overlap"`.

### 12.5 `reconstruct_byte_walk(document) -> bytes`

Recurses `document["file"]`; for every chunk the entries must tile
`0..byte_length` exactly:

| Violation | `ValueError` |
|---|---|
| entry `at` ≠ running cursor | `f"byte walk for {chunk} skips or overlaps at 0x{cursor:04X}"` |
| nested/hex bytes length ≠ `byte_length` | `"byte-walk entry length does not match its bytes"` |
| final cursor ≠ chunk `byte_length` | `f"byte walk for {chunk} ends at 0x{...}, not 0x{...}"` |

Concatenation order is the `bytes` list order; nested chunks
contribute their own reconstruction. A correct document therefore
round-trips the source file **byte for byte**.

## 13. `podsync.itunesdb_parser.byte_walk`

Indexes a (possibly huge) byte-walk JSON on disk without `json.load`.
Private marker: `_CHUNK_MARKER = b'"chunk": "'`.

### 13.1 Dataclasses

| Class | Fields (all `frozen=True, slots=True`) |
|---|---|
| `ByteWalkChunkIndexEntry` | `json_offset: int`, `chunk_type: str`, `caption: str`, `file_offset: int`, `byte_length: int` |
| `ByteWalkDocumentIndex` | `entries: list[ByteWalkChunkIndexEntry]`, `children_by_parent: dict[int, tuple[ByteWalkChunkIndexEntry, ...]]` |
| `ByteWalkChunkLoad` | `chunk: dict[str, Any]`, `was_cached: bool` |

### 13.2 Indexing

`index_byte_walk_json(path) -> list[ByteWalkChunkIndexEntry]`:

* mmap the file read-only; repeatedly `find(_CHUNK_MARKER)`; decode the
  JSON string after the marker (escape-aware) as `chunk_type`;
* within a `min(len, marker + 16_384)` window read `"caption": `,
  `"file_offset": ` (string, later `int(s, 0)`), `"byte_length": `
  (digits);
* chunk object start = `rfind(b"{", marker - 256, marker)`; not found →
  `ValueError("could not find the start of a byte-walk chunk")`;
* missing caption/offset/length properties →
  `ValueError(f"byte-walk chunk is missing {name!r}")`; non-digit length →
  `ValueError(f"byte-walk chunk has an invalid {name!r}")`;
* unterminated string → `ValueError("unterminated JSON string in byte-walk document")`;
* **no entries at all** → `ValueError("this file does not contain a podsync byte-walk JSON document")`.

`build_chunk_hierarchy(entries) -> dict[int, tuple[entry, ...]]`:
sorts by `(file_offset, -byte_length, json_offset)`, uses a containment
stack (`parent.file_offset ≤ child.start` and `child.end ≤ parent.end`,
strict); every entry — including leaves — gets a (possibly empty) tuple
keyed by its `json_offset`.

`index_byte_walk_document(path) -> ByteWalkDocumentIndex` = both of the
above.

### 13.3 On-demand loading

`load_indexed_chunk(path, entry) -> dict`: brace-match from
`entry.json_offset` with in-string/escape tracking
(unterminated → `ValueError("unterminated JSON object in byte-walk document")`),
`json.loads` the slice, then verify
`parsed.get("chunk") == entry.chunk_type` else
`ValueError("byte-walk index does not match the selected chunk")`.

`load_indexed_chunk_outline(path, entry, children) -> dict`: reads only
the leading leaf entries of the chunk's `"bytes": [` array (found
within 16 384 bytes of the object start; missing →
`ValueError("byte-walk chunk is missing its bytes array")`), stopping
at the first nested-chunk item when `children` is non-empty. Returns:

```json
{"chunk": ..., "caption": ..., "file_offset": "0x%X", "byte_length": ...,
 "bytes": [ ...direct leaf entries..., ...child references... ]}
```

**Note the outline formats `file_offset` as `f"0x{n:X}"` (no padding),
while forensics uses `f"0x{n:04X}"`** — both spellings occur in
expected data; keep them exactly as stated.

Child reference entry:

```json
{"at": "0x{child.file_offset - parent.file_offset:X}", "byte_length": n,
 "chunk": {"chunk": ..., "caption": ..., "file_offset": "0x{X}", "byte_length": n},
 "indexed_chunk": <ByteWalkChunkIndexEntry>}
```

The nested `chunk` here has **no `bytes` key** (deliberate — descendants
stay on disk). Related errors:
`"byte-walk bytes array contains an invalid entry"`,
`"... a non-object entry"`,
`"byte-walk bytes array contains an invalid object key"`,
`"... an invalid object property"`,
`"... an invalid object separator"`,
`"unterminated byte-walk bytes array"`,
`"unterminated JSON value in byte-walk document"`.

### 13.4 `ByteWalkChunkCache`

```python
ByteWalkChunkCache(*, max_entries: int = 16, max_bytes: int = 16 * 1024 * 1024)
```

* `max_entries < 1` → `ValueError("max_entries must be positive")`;
  `max_bytes < 1` → `ValueError("max_bytes must be positive")`.
* Key: `("full" | "outline", Path(path).resolve(), entry.json_offset)`
  — outline and full loads of the same chunk are cached separately.
* `load(path, entry) -> ByteWalkChunkLoad`,
  `load_outline(path, entry, children) -> ByteWalkChunkLoad`.
* Hit → `move_to_end` (LRU) and `was_cached=True` (same object
  identity on repeat calls).
* Miss → first requester becomes the loader and calls
  `load_indexed_chunk`/`load_indexed_chunk_outline`; concurrent
  requesters block on an `Event` and receive the same chunk with
  `was_cached=True` (**coalescing — the loader runs exactly once**).
  Loader exceptions are stored and re-raised in every waiter.
  A waiter that wakes with neither value raises
  `RuntimeError("chunk load completed without a result")`.
* Storage: retained size estimated by recursive `sys.getsizeof`
  (`_estimate_cache_bytes`, cycle-safe). If a single value exceeds
  `max_bytes` it is returned but **not** cached. Eviction pops the
  least-recently-used entries while `len > max_entries` or
  `cached_bytes > max_bytes`.
* `clear()` (under the lock): drops items, in-flight entries, byte
  counter, and **bumps a generation counter**. A load that started
  before `clear()` still returns its chunk to its own caller
  (`was_cached=False`) but is *not* stored; a subsequent `load` for the
  same key runs the loader again (observed `calls == 2` in tests) — an
  old document can never repopulate a cache after a new one is opened.

### 13.5 `hex_interpretations(hex_text: str) -> dict[str, str]`

Normalization: lower-case, then remove `"0x"`, spaces, `"\n"`, `"\t"`,
`":"`, `","` — pasted hexdumps work unchanged.

| Condition | Behavior |
|---|---|
| empty after normalize | `{}` |
| odd digit count | `ValueError("hex input has an odd number of digits")` |
| non-hex characters | `ValueError("hex input contains a non-hexadecimal character")` |

Result keys in this order (values are `str`):

1. `"Byte count"` — `str(len(raw))`
2. `"ASCII"` — `raw.decode("ascii", errors="replace")`
3. `"UTF-8"` — `raw.decode("utf-8", errors="replace")`
4. `"UTF-16 LE"` — decode when `len(raw) % 2 == 0`, else `"— (odd byte count)"`
5. `"UTF-16 BE"` — same rule
6. for `width in (1, 2, 4, 8)` when `len(raw) == width`:
   `"Unsigned {bits}-bit LE"`, `"Signed {bits}-bit LE"`, and (only when
   `width > 1`) `"Unsigned {bits}-bit BE"`, `"Signed {bits}-bit BE"`
7. when `len(raw) == 4`: `"Mac epoch timestamp (u32 LE)"` — ISO-8601
   UTC computed as `datetime(1904, 1, 1, tzinfo=UTC) + seconds`

Example: `hex_interpretations("0x19:00")` →
`Byte count == "2"`, `Unsigned 16-bit LE == "25"`,
`Unsigned 16-bit BE == "6400"`, plus the UTF-16 rows.

## 14. `podsync.itunesdb_parser.ipod_library`

### 14.1 `load_ipod_library(itunesdb_path: str, merge_playcounts: bool = True) -> dict | None`

| Stage | Behavior |
|---|---|
| Guard | falsy path or missing file → `None` (no log) |
| DB timezone offset | read first `0x70` bytes; `database_offset = i32 LE @ 0x6C` when `len ≥ 0x70 and header[:4] == b"mhbd"`, else `None` |
| Device context | `read_device_time_context(<iPod root>, database_offset=...)` where the root is `dirname³(itunesdb_path)` (imported **inside** the function) |
| TZ changed? | `timezone_changed_since_database(context, database_offset)` |
| Parse context | `DeviceTimeContext.fixed_offset(database_offset)` when the TZ changed **and** `database_offset is not None`, else the device context |
| Raw parse | `parse_itunesdb(itunesdb_path, time_context=database_time_context)` — **import-time** binding |
| Flatten | `data = extract_datasets(raw)` |
| Inlining | track strings/extras → artwork hydration → album strings → playlist strings/extras/items → artist strings (§14.2) |
| Play counts | when `merge_playcounts`: read sibling `Play Counts`, `merge_playcounts(tracks, entries, time_context=device_time_context)`; any failure inside → DEBUG `Play Counts merge skipped` (exc_info), never raises |
| Flag | `data["playcounts_timezone_changed"] = timezone_changed` — **always set**, even when merging is off |
| OTG | `load_otg_playlists(dirname(itunesdb_path), data.get("mhlt", []))`; when non-empty `data.setdefault("mhlp", []).extend(otg)` |
| Return | the dict; **any** `Exception` → ERROR `Error parsing iTunesDB` (exc_info=True) → `None` |

Output keys: every MHBD header field (minus `children`), one key per
dataset (`mhlt`, `mhla`, `mhlp`, `mhlp_podcast`, `mhlp_smart`,
`mhsd_type_6/7/8/9/10` as present), `playcounts_timezone_changed`, and
optionally `mhlp` extended with OTG rows. `data["mhbd"]` does **not**
exist — the MHBD dict *is* the result.

### 14.2 Inlining helpers (private, in-place mutations)

| Helper | Behavior |
|---|---|
| `_inline_track_strings(data)` | for each `mhlt` row: **pop** `children`; `update(extract_mhod_strings(children))` (last duplicate type wins, order = file order); `update(extract_track_extras(children))` (adds `chapter_data` when MHOD 17 present, even with empty chapters); `filetype` int > 0 → ASCII string via `filetype_to_string` (else unchanged); `sort_mhod_indicators` `bytes` → `list[int]`. Numeric fields already transformed by `read_transform`s. |
| `_inline_album_strings(data)` | for each `mhla` row: pop `children`, merge MHOD strings |
| `_inline_playlist_strings(data)` | for keys `mhlp`/`mhlp_podcast`/`mhlp_smart` with dataset types 2/3/5: `setdefault("_mhsd_dataset_type", type)` and `setdefault("_mhsd_result_key", key)`; **pop** `mhod_children`, merge strings + `extract_playlist_extras`; **pop** `mhip_children` → new `items` list of the inner MHIP dicts, each updated with `extract_playlist_item_extras(item.get("children", []))` (adds `podcast_group_title` from a type-1 MHOD). The MHIP dicts keep their own `children` key. |
| `_inline_artist_strings(data)` | for each `mhsd_type_8` row: pop `children`, merge MHOD strings (type 300 → `"Artist"`) |
| `_merge_play_counts(data, itunesdb_path, time_context)` | sibling path = `join(dirname(itunesdb_path), "Play Counts")`; `parse_playcounts` → `None` = skip; else `merge_playcounts(data.get("mhlt", []), entries, time_context=...)` |

Note: a string-keyed MHOD with a `"string"` value is inlined under its
`mhod_type_map` name — including type 32 (hex under
`"Unknown for Video Track"`) and types 15/16 (podcast URLs). Types
without a `mhod_type_map` name, or non-string MHODs, are not inlined
here (playlist-level structured data flows through
`extract_playlist_extras` instead).

### 14.3 Extraction helpers (recap — implemented in `podsync.itunesdb_shared.extraction`, needed here to predict output)

| Function | Output |
|---|---|
| `extract_datasets(mhbd)` | MHBD fields minus `children`, plus one key per `mhsd` child: `chunk_type_map[dataset_type]`; unknown `dataset_type` → skipped; type 9 → `{"raw_payload_hex": bytes.hex(), "genius_cuid": str}`; empty children → `[]`; otherwise the list chunk's items flattened, each dict row getting `setdefault("_mhsd_dataset_type", type)` and `setdefault("_mhsd_result_key", key)` (non-dict rows pass through untouched) |
| `extract_mhod_strings(children)` | `{mhod_type_map[type]: mhod["string"]}` — requires both a mapped name and a `"string"` key; unknown/unmapped types skipped |
| `extract_track_extras(children)` | `{"chapter_data": <dict>}` from the **last** MHOD 17 whose `data` is a dict; else `{}` |
| `extract_playlist_extras(children)` | optional keys: `smart_playlist_data` (50), `smart_playlist_rules` (51), `library_indices` (**list**, appended per 52), `playlist_property_plist` (55) + `playlist_description` (only when non-empty), `playlist_prefs` (100), `playlist_settings` (102) |
| `extract_playlist_item_extras(children)` | `{"podcast_group_title": str}` when a type-1 MHOD title exists, else `{}` |

### 14.4 Neighbor dependencies and graceful degradation

| Dependency | Module | Behavior when absent/failing |
|---|---|---|
| `read_device_time_context`, `timezone_changed_since_database`, `DeviceTimeContext` | `podsync.itunesdb_shared.device_time` (chapter 03) | missing/unreadable `iPod_Control/Device/Preferences` → falls back to `DeviceTimeContext.fixed_offset(database_offset)` when known, else UTC; parse still succeeds |
| `parse_artworkdb` | `podsync.artworkdb_parser.parser` (chapter 05) | imported lazily inside `_build_song_to_artwork_id`; missing file, import failure, or parse error → `{}` → hydration no-op returning 0 |
| `extract_*`, `filetype_to_string` | `podsync.itunesdb_shared.extraction` / `field_base` (chapter 03) | part of the shared package; `test_package_imports` covers importability |

### 14.5 Dependency recap: `DeviceTimeContext` (chapter 03 owns the full spec)

Frozen dataclass `(timezone: tzinfo, name: str, source: str,
city_id: int | None = None)` with `utc()`, `fixed_offset(seconds, *,
source="database_header")` (raises `ValueError` beyond ±86 400 s),
`from_timezone_name(...)` (raises `ValueError` for unknown zones), and
conversions:

* `mac_to_unix(ts)` → `0` for `ts ≤ 0`; else interpret the value as
  seconds since 1904-01-01 **in this context's timezone** and return
  Unix seconds.
* `unix_to_mac(ts)` → `0` for `ts ≤ 0`; else raises
  `MacTimestampOutOfRangeError` (a `ValueError`) when the result
  leaves `(0, 0xFFFF_FFFF]` — never silently clamped.

Module-level: `current_device_time_context()` (active or UTC),
`active_device_time_context()`, `use_device_time_context(ctx)`
context manager, `MAC_EPOCH_OFFSET = 2_082_844_800`,
`MAC_U32_MAX = 0xFFFF_FFFF`. When `parse_itunesdb` is called without
`time_context`, timestamps convert under the active context, default
UTC.

## 15. Error & edge-case matrix

| Scenario | Behavior |
|---|---|
| Empty file | `CorruptHeaderError(0, "empty file")` from `parse_itunesdb` |
| File shorter than 12 bytes (non-empty) | `InsufficientDataError(0, 12, len)` |
| Non-ASCII tag bytes | `CorruptHeaderError(offset, "chunk type bytes are not valid ASCII: b'…'")` |
| `< 12` bytes at a child offset | `InsufficientDataError(child_offset, 12, remaining)` |
| Declared `header_length` beyond buffer | `struct.error` propagates (not wrapped) |
| Unknown chunk tag | counted, skipped by `offset + length`, body kept as `{"chunk_type","header","body"}`, single WARNING summary per top-level parse |
| `child_count` larger than available bytes | surfaces as `InsufficientDataError`/`struct.error` from the child loop |
| Container with trailing bytes after children | trailer preserved in `_raw_chunk.unparsed_bytes`; `next_offset` still `offset + chunk_length` for byte-length chunks |
| MHOD `string_length` larger than the chunk | reads into subsequent buffer bytes (slice clamp only at EOF) — no error |
| MHOD type 50 body < 12 | `{}` + WARNING |
| SLst body < 136 or bad magic | `{}` + WARNING |
| SLst rule truncated (< 68 B payload, non-group) | `raw_data` preservation, consumed `56 + data_length` |
| Nested SLst group payload truncated to < 136 | no `group` key — leaf path, `raw_data == b"SLst…"` |
| Chapter body < 12 / bad `sean` | `{"chapters": []}` (+WARNING where specified), no unk keys when preamble missing |
| MHOD unknown type | `string == ""`, body only in `_raw_chunk` |
| `decompress_itunescdb` on plain DB | returned unchanged at steps 1/3 |
| iTunesCDB with flag=2 but invalid zlib | returned unchanged + no debug line |
| Play Counts missing / short / bad magic / truncated | `None` + the WARNINGs of §9.3 (never raises) |
| Play Counts with fewer entries than tracks | merged prefix + WARNING; trailing tracks get `recent_*=0` only |
| OTG base file missing | `[]` (no numbered scan) |
| OTG hole in numbering | scan stops at the gap |
| OTG empty/all-zero/faulty file | `None` for that file; overall list unaffected |
| ArtworkDB absent or unparsable | `{}` → hydration returns 0 (INFO lines absent) |
| Byte-walk JSON without chunks | `ValueError("this file does not contain a podsync byte-walk JSON document")` |
| Byte-walk reconstruction mismatch | `ValueError("byte-walk reconstruction does not match the source file")` |
| `hex_interpretations("123")` | `ValueError("hex input has an odd number of digits")` |
| `parse_itunesdb` wrong input type | `TypeError` with the exact message of §8.2 |
| Any failure inside `load_ipod_library` | ERROR log + `None` |

## 16. Scope exclusions

Everything below is **absent** from `podsync.itunesdb_parser`; call
sites must not exist, or — where an entry point is implied by
neighboring layers — raise a clear `NotImplementedError` /
`RuntimeError` (see chapter 01 §5):

1. **`commit_playcounts_if_needed` / `_commit_playcounts_guarded`** —
   committing play counts as a separate write step is *not* part of
   this package (it belongs to `podsync.sync._db_io`, chapter 08).
   In-scope here: `parse_playcounts` + `merge_playcounts` during a
   full read, and nothing else.
2. **`pc_track_to_info`** — PC-library track conversion does not exist
   anywhere in this package (chapter 08's
   `sync._track_conversion` owns `track_dict_to_info`).
3. **SQLite nano 5G–7G databases** — no `sqlitedb_writer`, no
   `write_sqlite_databases` path, no sqlite imports; `parse_itunesdb`
   only ever reads the classic binary file family.
4. **No GUI / application / podcasts / transcoder imports** — no module
   of `podsync.itunesdb_parser` may import `podsync.gui`,
   `podsync.application`, podcast modules, `sqlitedb_writer`, or any
   sync/transcoder code. The only cross-package import in the whole
   package is the **lazy** `podsync.artworkdb_parser.parser` inside
   `_build_song_to_artwork_id` (plus `podsync.itunesdb_shared.*`).
5. **No writing of device databases** — this package never writes
   iTunesDB/ArtworkDB bytes. The only filesystem side effects are
   `forensics.export_forensic_json` (writes a JSON report the caller
   asked for) and `otg.delete_otg_files` (removes the base
   `OTGPlaylistInfo` when the sync layer requests it).
6. **No network access, no import-time I/O.**

## 17. Logging & side-effect inventory

| Module | Level | Message (format) |
|---|---|---|
| `chunk_parser` | WARNING | `iTunesDB contained %d unknown chunk(s); ignored while parsing. Examples: %s%s` (once per top-level parse, only when unknowns exist; nothing per-chunk) |
| `parser` | DEBUG | `iTunesCDB decompressed: %d -> %d payload bytes` |
| `mhod_parser` | WARNING | `MHOD50 (SPLPref) body too short: %d bytes` |
| `mhod_parser` | WARNING | `MHOD51 (SPLRules) body too short: %d bytes` |
| `mhod_parser` | WARNING | `MHOD51: expected SLst magic, got %r` |
| `mhod_parser` | WARNING | `MHOD52 (sorted index) body too short: %d bytes` |
| `mhod_parser` | WARNING | `MHOD53 (jump table) body too short: %d bytes` |
| `mhod_parser` | WARNING | `MHOD17 (chapter data) too short for preamble: %d bytes` |
| `mhod_parser` | WARNING | `Chapter data: invalid 'sean' atom size: %d` |
| `mhod_parser` | WARNING | `Chapter data: expected 'sean' atom, got %r` |
| `playcounts` | DEBUG | `No Play Counts file at %s` |
| `playcounts` | WARNING | `Could not read Play Counts file: %s` |
| `playcounts` | WARNING | `Play Counts file too small (%d bytes)` |
| `playcounts` | WARNING | `Play Counts file bad magic: %r (expected b'mhdp')` |
| `playcounts` | WARNING | `Play Counts file truncated: %d bytes < expected %d` |
| `playcounts` | INFO | `Play Counts: header=%d, entry_len=%d, entries=%d` |
| `playcounts` | INFO | `Play Counts: %d / %d entries have activity` |
| `playcounts` | WARNING | `Track count (%d) != Play Counts entry count (%d); merging first %d` |
| `playcounts` | INFO | `Merged Play Counts: %d plays, %d skips, %d ratings across %d tracks` |
| `otg` | WARNING/ERROR-free skips | the six `OTG: …` warnings of §10.1 |
| `otg` | INFO | `OTG: imported '%s' (%d tracks) from %s` |
| `otg` | INFO / WARNING | `Deleted OTGPlaylistInfo` / `Could not delete OTGPlaylistInfo: %s` |
| `artwork_links` | DEBUG | `Could not parse ArtworkDB for artwork links: %s` |
| `artwork_links` | INFO | `Corrected stale track artwork ref for db_track_id=%d: %d -> %d` |
| `artwork_links` | INFO | `Normalized %d track artwork refs from ArtworkDB song links` |
| `ipod_library` | DEBUG | `Play Counts merge skipped` (exc_info) |
| `ipod_library` | ERROR | `Error parsing iTunesDB` (exc_info) |

Stateful side effects:

| State | Scope | Reset |
|---|---|---|
| unknown-chunk `Counter` | module-global in `chunk_parser` | `reset_unknown_chunk_summary()` at the start of every `parse_itunesdb` |
| raw-preservation flag | `ContextVar` in `_parsing` | restored on context exit |
| device time context | `ContextVar` in `device_time` | restored on context exit |

`forensics` and `byte_walk` are read-only aside from
`export_forensic_json`'s output file; `ByteWalkChunkCache` holds only
in-memory state owned by its instance.

## 18. Boundaries with other chapters

| Topic | Where it lives |
|---|---|
| Full `*_defs` tables, label maps (`SPL_FIELD_MAP`, `SPL_ACTION_MAP`, `MEDIA_TYPE_MAP`, `PLAYLIST_SORT_ORDER_MAP`, …), writer-side header sizes | chapter 03 |
| Writing bytes back (`write_mhbd`, MHOD limits, hashes 58/72/AB) | chapter 04 |
| `parse_artworkdb` consumed by §11 | chapter 05 |
| Device discovery, `resolve_itdb_path`, write guard | chapter 06/07 |
| `read_existing_database` / `write_database` / play-count commit policy / OTG deletion after write / `track_dict_to_info` | chapter 08 (`sync._db_io`, `sync._track_conversion`) |
