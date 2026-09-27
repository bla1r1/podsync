# Chapter 05 — ArtworkDB: on-disk format, parser, codecs, and writer

This chapter owns the **ArtworkDB binary format** (chunk tree, field tables, string
encodings), the **ITHMB image-file format** (naming, layout, pixel codecs), and the
`podsync.artworkdb_*` packages that parse and produce them.

All multi-byte integers are **little-endian** unless a table says
otherwise. All offsets in field tables are **relative to the start of the chunk**
(i.e. relative to `offset` in the parser functions).

Dependencies allowed for these modules: Python standard library and **Pillow**
(`PIL`). `numpy` and `mutagen` are *not* available in this project — every algorithm
below is specified arithmetically so it can be implemented with Pillow/struct/
plain Python while remaining bit-exact for packed formats (see §9.9 for the
tolerance on YUV formats).

---

## 1. Module map

| Module path | Responsibility |
|---|---|
| `podsync.artworkdb_shared.constants` | Chunk header sizes, dataset/mhod enums, MHOD type table |
| `podsync.artworkdb_shared.binary` | `ChunkHeader`, chunk header/length validators, `read_u16/i16/u32/u64` |
| `podsync.artworkdb_shared.mhod` | MHOD type lookup, string body encode/decode |
| `podsync.artworkdb_shared.mhni` | MHNI field reader, expected-size math, format inference |
| `podsync.artworkdb_shared.mhlf` | `extract_format_ids` — scan MHIF correlation IDs |
| `podsync.artworkdb_shared.ithmb_paths` | `F{id}_{N}.ithmb` filename generation/normalization |
| `podsync.artworkdb_shared.__init__` | Docstring only (empty package) |
| `podsync.artworkdb_parser.parser` | `parse_artworkdb` entry point |
| `podsync.artworkdb_parser.chunk_parser` | Tag dispatch (`parse_chunk`) |
| `podsync.artworkdb_parser.{mhfd,mhsd,mhli,mhii,mhod,mhni}_parser` | Per-chunk parsers |
| `podsync.artworkdb_parser.constants` | Backwards-compatible aliases of shared constants |
| `podsync.artworkdb_parser.__init__` | Exports `parse_artworkdb` only |
| `podsync.artworkdb_writer.artwork_types` | Dataclasses (`ArtworkEntry`, payload/ref types) |
| `podsync.artworkdb_writer.artworkdb_chunks` | Chunk serialization, `build_artworkdb`, `read_existing_artwork` |
| `podsync.artworkdb_writer.ithmb_codecs` | Format-aware encode/decode of pixel payloads |
| `podsync.artworkdb_writer.rgb565` | RGB565 helpers, device format-table lookups, image loading |
| `podsync.artworkdb_writer.art_extractor` | Embedded/folder artwork extraction, `art_hash` |
| `podsync.artworkdb_writer.artwork_writer` | `write_artworkdb` orchestration, `PendingArtworkWrite` |
| `podsync.artworkdb_writer.__init__` | Public re-exports: `write_artworkdb`, `ArtworkEntry`, `extract_art`, `art_hash`, `convert_art_for_ipod`, `image_from_bytes`, `rgb888_to_rgb565`, `get_artwork_formats`, the `IPOD_*_FORMATS` tables and `ALL_KNOWN_FORMATS` (§10.1), plus `ITHMB_FORMAT_MAP`, `ITHMB_SIZE_MAP`, `ithmb_formats_for_device` from `podsync.device` |

Cross-package consumption from `podsync.device` (the registry itself is
**chapter 06**; only the values this chapter depends on are copied here, §4).

---

## 2. The ArtworkDB chunk format

### 2.1 Generic chunk header

Every chunk starts with a 12-byte generic header:

| Offset | Size | Type | Field | Meaning |
|---|---|---|---|---|
| 0 | 4 | bytes | `tag` | Four-byte ASCII tag (`mhfd`, `mhsd`, `mhli`, `mhii`, `mhod`, `mhni`, `mhla`, `mhlf`, `mhif`, …) |
| 4 | 4 | u32 LE | `header_size` | Byte offset where this chunk's children/body start |
| 8 | 4 | u32 LE | `length_or_count` | **Total chunk length in bytes** for container chunks, **child count** for list chunks (see §2.3) |

Constants (`podsync.artworkdb_shared.binary`):
`GENERIC_CHUNK_HEADER_SIZE = 12`, `MIN_TYPED_CHUNK_HEADER_SIZE = 14`.

### 2.2 Fixed header sizes

From `podsync.artworkdb_shared.constants`:

| Constant | Value |
|---|---|
| `MHFD_HEADER_SIZE` | 132 |
| `MHSD_HEADER_SIZE` | 96 |
| `MHLI_HEADER_SIZE` | 92 |
| `MHLA_HEADER_SIZE` | 92 |
| `MHLF_HEADER_SIZE` | 92 |
| `MHII_HEADER_SIZE` | 152 |
| `MHOD_HEADER_SIZE` | 24 |
| `MHNI_HEADER_SIZE` | 76 |
| `MHIF_HEADER_SIZE` | 124 |

### 2.3 Chunk tree

```
mhfd                                    (root, whole file)
├── mhsd datasetType=1 (IMAGE_LIST)
│   └── mhli                            (image list; +8 field = mhii count)
│       ├── mhii                        (one per live artwork entry)
│       │   ├── mhod type=2 (container) ── mhni
│       │   │                              └── mhod type=3 (string) = ":F<fmt>_<N>.ithmb"
│       │   ├── mhod type=2 ── mhni …    (one per format the entry carries)
│       │   └── …
│       └── mhii …
├── mhsd datasetType=2 (PHOTO_ALBUM_LIST)
│   └── mhla                            (empty; +8 field = album count = 0)
└── mhsd datasetType=3 (FILE_LIST)
    └── mhlf                            (file list; +8 field = mhif count)
        ├── mhif                        (one per image format in use)
        └── mhif …
```

Notes:

* `mhii` children are always `mhod` chunks. Image pixel data never lives in
  ArtworkDB — only references (format id + byte offset + size) into `.ithmb` files.
* `mhod type=2` (thumbnail) and `type=5` (full-res) and `type=6` are
  **containers**: their body (starting at `header_size`) holds one `mhni` child.
  The writer only ever emits type-2 containers; the parser handles all.
* Observed on an iTunes-written ArtworkDB (iPod Video 5.5G, mhfd `+16` = 6):
  **every** `mhii` carries one `mhod type=6` container whose child is an
  `mhaf` chunk (`+4` = 0x60, `+8` = 0x3C, body zero), placed after the
  type-2 containers; some `mhii` (with a valid `songId`) carry **only** that
  container and no image formats. The generic parser returns `{}` for the
  `mhaf`; `read_existing_artwork` skips type 6 and drops entries without
  formats (§7.4); the writer never emits type 6. iTunes also packed 1 884
  frames into a single `F1028_1.ithmb` (37.7 MB) — larger than the writer's
  32 MB planning budget, so the writer places new frames in the next shard
  (§12.8).
* `mhod type=1` (album name) and `type=3` (file name) are **strings**.
* Tags `mhba`, `mhia` (photo album items) are recognized by the parser but
  parsed as empty dicts `{}`. `mhaf` likewise returns `{}`. Any other tag
  raises (§5.5).

### 2.4 `mhfd` — data file (root) header, 132 bytes

| Offset | Size | Type | Writer value | Parser key |
|---|---|---|---|---|
| 0 | 4 | tag | `b"mhfd"` | — |
| 4 | 4 | u32 | `132` | — |
| 8 | 4 | u32 | total file length | — |
| 12 | 4 | u32 | 0 | `unk1` (always 0) |
| 16 | 4 | u32 | `2` | `unk2` (1 until iTunes 4.9, 2 after) |
| 20 | 4 | u32 | `len(datasets)` = 3 | `childCount` |
| 24 | 4 | u32 | 0 | `unk3` (always 0) |
| 28 | 4 | u32 | `next_mhii_id` | `next_mhii_id` (ID of last mhii + 1) |
| 32 | 8 | u64 | copied from reference mhfd if provided | `unk4` |
| 40 | 8 | u64 | copied from reference mhfd if provided | `unk5` |
| 48 | 4 | u32 | `2` | `unk6` (always 2) |
| 52 | 4 | u32 | 0 | `unk7` (always 0) |
| 56 | 4 | u32 | 0 | `unk8` (always 0) |
| 60 | 4 | u32 | copied from reference mhfd if provided | `unk9` |
| 64 | 4 | u32 | copied from reference mhfd if provided | `unk10` |
| 68..132 | 64 | — | zero | (not parsed) |

Children start at `offset + header_size`. The parser iterates `childCount`
times, each child returning `{"nextOffset", "result", "datasetType"}` (from
`mhsd`), and stores them under `CHUNK_TYPE_MAP[datasetType]`
(`"mhli"` / `"mhla"` / `"mhlf"`).

### 2.5 `mhsd` — data set, 96 bytes

| Offset | Size | Type | Meaning | Parser key |
|---|---|---|---|---|
| 0 | 4 | tag | `b"mhsd"` | — |
| 4 | 4 | u32 | `96` | — |
| 8 | 4 | u32 | total length (header + child) | — |
| 12 | **2** | **u16** | dataset type (1/2/3) — **u16, not u32** | `datasetType` |
| 14 | 82 | — | padding | — |

Exactly one child chunk follows at `offset + header_size`. The parser unwraps
the child and returns `{"datasetType": int, "result": unwrapped, "nextOffset": offset + chunk_length}`.

### 2.6 `mhli` — image list, 92 bytes

| Offset | Size | Type | Meaning |
|---|---|---|---|
| 0 | 4 | tag | `b"mhli"` |
| 4 | 4 | u32 | `92` |
| 8 | 4 | u32 | **number of `mhii` children** (not a byte length) |

Children follow at `offset + header_size`; the parser loops that many times and
returns `{"nextOffset": <after last child>, "result": [mhii_dict, ...]}`.

### 2.7 `mhla` / `mhlf` — album list / file list, 92 bytes

Identical layout: tag, `header_size = 92`, `+8` = **child count**
(0 for `mhla`; number of `mhif` for `mhlf`). The generic parser returns `{}`
for both tags (no child parsing). The writer always emits an empty `mhla`.

### 2.8 `mhii` — image item, 152 bytes

| Offset | Size | Type | Writer value | Parser key |
|---|---|---|---|---|
| 0 | 4 | tag | `b"mhii"` | — |
| 4 | 4 | u32 | `152` | — |
| 8 | 4 | u32 | total length | — |
| 12 | 4 | u32 | number of `mhod` children (one per format) | (child count) |
| 16 | 4 | u32 | `entry.img_id` | `img_id` |
| 20 | 8 | u64 | `entry.db_track_id` | `songId` |
| 28 | 4 | u32 | 0 | `unk1` (always 0) |
| 32 | 4 | u32 | 0 | `rating` (iPhoto rating ×20; 0 in ArtworkDB) |
| 36 | 4 | u32 | 0 | `unk2` |
| 40 | 4 | u32 | 0 | `originalDate` |
| 44 | 4 | u32 | 0 | `exifTakenDate` |
| 48 | 4 | u32 | `entry.src_img_size` | `srcImgSize` (bytes of original source image) |
| 52..152 | 100 | — | zero | (not parsed) |

`songId` is the unique ID matching `db_track_id` in iTunesDB — this is the
link used to map ArtworkDB items to tracks. First `mhii` id is normally 0x40
on devices, but the writer assigns fresh ids starting at `start_img_id`
(default 100).

### 2.9 `mhod` — data object, 24-byte header

| Offset | Size | Type | Meaning | Parser key |
|---|---|---|---|---|
| 0 | 4 | tag | `b"mhod"` | — |
| 4 | 4 | u32 | `24` | — |
| 8 | 4 | u32 | total length (header + body) | — |
| 12 | 2 | u16 | mhod type (see §2.11) | `mhodType` |
| 14 | 1 | u8 | 0 (not parsed) | — |
| 15 | 1 | u8 | padding length to 4-byte multiple; 0, 1 or 3 (not parsed) | — |

Body depends on type:

* **String** (types 1, 3): body layout in §2.12. Parser result:
  `{"mhodType": t, <type name>: "<decoded string>"}`.
* **Container** (types 2, 5, 6): body starts at `offset + header_size` and
  holds one child chunk (an `mhni`). Parser result:
  `{"mhodType": t, <type name>: {<raw child wrapper with nextOffset/result>}}`
  — note the *wrapper* dict, not the unwrapped result.
* **Unknown type**: result `{"mhodType": t, "_unknown": True}` (no body parse).
* Any type whose table entry is neither `"String"` nor `"Container"`:
  `{"mhodType": "ERROR"}` (unreachable with the current table).

In all cases the mhod returns `{"nextOffset": offset + chunk_length, "result": ...}`.

### 2.10 `mhni` — image name, 76 bytes

| Offset | Size | Type | Writer value | Parser key / `MhniFields` field |
|---|---|---|---|---|
| 0 | 4 | tag | `b"mhni"` | — |
| 4 | 4 | u32 | `76` | — |
| 8 | 4 | u32 | total length | — |
| 12 | 4 | u32 | `1` (child count) | `child_count` |
| 16 | 4 | u32 | format (correlation) id | `format_id` → `correlationID` |
| 20 | 4 | u32 | byte offset of frame in `.ithmb` file | `ithmb_offset` → `ithmbOffset` |
| 24 | 4 | u32 | frame size in bytes | `image_size` → `imgSize` |
| 28 | 2 | i16 | vertical padding (pixels, ≥ 0) | `vertical_padding` → `verticalPadding` |
| 30 | 2 | i16 | horizontal padding (pixels, ≥ 0) | `horizontal_padding` → `horizontalPadding` |
| 32 | 2 | u16 | visible image height | `image_height` → `imageHeight` |
| 34 | 2 | u16 | visible image width | `image_width` → `imageWidth` |
| 36 | 4 | u32 | 0 | `unk1` (always 0) |
| 40 | 4 | u32 | same as `imgSize` (seen after iTunes 7.4) | `image_size_2` → `imgSize2` |

One child `mhod type=3` follows at `+76` whose string value is
`":" + filename` (leading colon), e.g. `:F1055_1.ithmb`.

Derived (properties on `MhniFields`, also exposed by the parser):

* `estimated_pixmap_height = vertical_padding + image_height`
* `estimated_pixmap_width  = horizontal_padding + image_width`

Parser result dict keys: `correlationID, ithmbOffset, imgSize,
verticalPadding, horizontalPadding, imageHeight, imageWidth, unk1, imgSize2,
estimatedPixmapHeight, estimatedPixmapWidth, image_format` (dict or None from
`infer_image_format`, §6.4), plus one key **per child mhod keyed by the integer
mhod type**, e.g. `3: {"mhodType": 3, "File Name": ":F1055_1.ithmb"}`.
Children are parsed `child_count` times starting at `offset + header_size`.

### 2.11 Enums and tables (`artworkdb_shared.constants`)

```text
class ArtworkDatasetType(IntEnum): IMAGE_LIST=1, PHOTO_ALBUM_LIST=2, FILE_LIST=3
class ArtworkMhodType(IntEnum):    ALBUM_NAME=1, THUMBNAIL_IMAGE=2, FILE_NAME=3,
                                   FULL_RES_IMAGE=5, UNKNOWN_CONTAINER_6=6

CHUNK_TYPE_MAP   = {1: "mhli", 2: "mhla", 3: "mhlf"}
IDENTIFIER_READABLE_MAP = {"mhfd": "Data File", "mhsd": "Data Set", "mhli": "Image List",
    "mhii": "Image Item", "mhni": "Image Name", "mhla": "Photo Album List",
    "mhba": "Photo Album", "mhia": "Photo Album Item", "mhlf": "File List",
    "mhif": "File List Item", "mhod": "Data Object"}
MHOD_TYPE_MAP = {1: {"type": "String",    "name": "Album Name"},
                 2: {"type": "Container", "name": "Thumbnail Image"},
                 3: {"type": "String",    "name": "File Name"},
                 5: {"type": "Container", "name": "Full Res Image"},
                 6: {"type": "Container", "name": "UNK MHOD 6"}}
IMAGE_CONTAINER_MHOD_TYPES = frozenset({2, 5, 6})
IMAGE_CONTAINER_NAMES      = ("Full Res Image", "Thumbnail Image", "UNK MHOD 6")
```

### 2.12 MHOD string body encoding

Body starts at `offset + header_size` (i.e. byte 24 of the mhod) and is
produced/consumed by `encode_mhod_string_body` / `decode_mhod_string_body`:

| Body offset | Size | Meaning |
|---|---|---|
| 0 | 4 | u32: string length **in encoded bytes** (excluding padding) |
| 4 | 1 | encoding byte: `2` = UTF-16-LE, `1` (or anything else) = UTF-8 |
| 5 | 3 | zero |
| 8 | 4 | zero (unknown/flags field) |
| 12 | N | encoded string bytes |
| 12+N | p | zero padding so the body length is a multiple of 4, `p = (4 - N%4) % 4` |

Encoding selection (`mhod_string_encoding`): **type 3 → `utf-16-le` /
byte 2; every other type → `utf-8` / byte 1.**

Decode rules (`decode_mhod_string_body(data, body_offset, body_end)`):

1. If `body_offset + 12 > body_end` → `None`.
2. `string_byte_length` = u32 at `body_offset`; `encoding` = byte at
   `body_offset + 4`.
3. Raw slice = `[body_offset+12 : min(body_end, body_offset+12+len)]`.
4. Decode with `errors="replace"` (UTF-16-LE if encoding == 2 else UTF-8),
   then `rstrip("\x00")`. Any `UnicodeError` → `None`.

`decode_mhod_string_chunk(data, offset, total_size)` (used when re-reading a
written file): returns `None` if `total_size < 36`; reads `header_size` at
`offset+4`, returns `None` if `header_size < 24` or `> total_size`; otherwise
decodes the body `[offset+header_size, offset+total_size)`.

### 2.13 The length-vs-count quirk (important)

The generic `+8` field means **total byte length** for `mhfd`, `mhsd`, `mhii`,
`mhod`, `mhni`, `mhif`, but means **child count** for `mhli`, `mhla`, `mhlf`.
The parser and the writer must both follow this rule; do not "normalize" it.

---

## 3. ITHMB files

### 3.1 File structure

An `.ithmb` file is a **headerless raw concatenation of encoded image frames**
for exactly one format id. Frame *N* starts at the byte offset recorded in its
`mhni.ithmb_offset`. There is no per-file header, index, trailer, or checksum —
ArtworkDB is the index.

### 3.2 Naming and path resolution (`artworkdb_shared.ithmb_paths`)

| Function | Signature | Behavior |
|---|---|---|
| `ithmb_filename` | `(format_id: int, index: int = 1) -> str` | `f"F{int(format_id)}_{int(index)}.ithmb"` |
| `normalize_ithmb_filename` | `(format_id: int, filename: str \| None, default_index: int = 1) -> str` | strip whitespace; `\` → `/`; if `:` present keep text after the **last** `:` (this strips the leading colon of `:F1055_1.ithmb`); if `/` present keep basename after last `/`; if the result is empty → `ithmb_filename(format_id, default_index)` |
| `ithmb_filename_from_path` | `(path, format_id, default_index=1) -> str` | `normalize_ithmb_filename(format_id, path, default_index)` |
| `ithmb_path_for_filename` | `(artwork_dir, format_id, filename) -> str` | `os.path.join(artwork_dir, normalize_ithmb_filename(format_id, filename))` |

On-device directory: `<ipod_path>/iPod_Control/Artwork/`. Files written there:
`ArtworkDB` and one or more `F<format_id>_<N>.ithmb` per format, `N ≥ 1`
ascending.

### 3.3 Size budget

`podsync.artworkdb_writer.artwork_writer.ITHMB_MAX_SIZE_BYTES = 32 * 1000 * 1000`
(32,000,000). It is a *planning* budget for mutable shards — see §12.8 and §12.11.

---

## 4. Format registry consumption (boundary with chapter 06)

Chapter 06 specifies `podsync.device`. This section lists exactly what the
ArtworkDB packages consume and copies the values they depend on.

### 4.1 `ArtworkFormat` (from `podsync.device.artwork_presets`)

Frozen dataclass, positional fields:

| # | Field | Type | Meaning |
|---|---|---|---|
| 1 | `format_id` | int | correlation id (MHNI `format_id` / MHIF id) |
| 2 | `width` | int | visible width in pixels |
| 3 | `height` | int | visible height in pixels |
| 4 | `row_bytes` | int | stored row stride **in bytes** (0 = unknown) |
| 5 | `pixel_format` | str | codec key, default `"RGB565_LE"` |
| 6 | `role` | str | default `"cover"`; values starting with `"photo"` enable the centered-crop quirk in §9.7 |
| 7 | `description` | str | default `""` |

### 4.2 `ITHMB_FORMAT_MAP` values (dependency copy)

`ITHMB_FORMAT_MAP` **is** `ARTWORK_FORMATS_BY_ID` (defined in chapter 06,
copied here because the codecs depend on these exact values):

| id | w×h | row_bytes | pixel_format | role | description |
|---|---|---|---|---|---|
| 1005 | 80×80 | 160 | RGB565_LE | photo_thumb | Nano 7G photo thumbnail |
| 1007 | 480×864 | 960 | RGB565_LE | photo_full | Nano 7G photo full screen |
| 1009 | 42×30 | 84 | RGB565_LE | photo_list | Photo list thumbnail |
| 1010 | 240×240 | 480 | RGB565_LE | cover_large | Nano 7G album art large |
| 1013 | 220×176 | 440 | RGB565_BE_90 | photo_full | Photo full screen (rotated) |
| 1015 | 130×88 | 260 | RGB565_LE | photo_preview | iPod 4G/5G preview |
| 1016 | 140×140 | 280 | RGB565_LE | cover_large | iPod 4G photo/color album art large |
| 1017 | 56×56 | 112 | RGB565_LE | cover_small | iPod 4G photo/color album art small |
| 1019 | 720×480 | 1440 | UYVY | tv_out | iPod 4G/5G NTSC TV output |
| 1020 | 220×176 | 440 | RGB565_BE_90 | photo_full | Photo full screen (alt rotated) |
| 1023 | 176×132 | 352 | RGB565_BE | photo_full | Nano full screen |
| 1024 | 320×240 | 640 | RGB565_LE | photo_full | 320x240 photo full screen |
| 1027 | 100×100 | 200 | RGB565_LE | cover_large | Nano album art large |
| 1028 | 100×100 | 200 | RGB565_LE | cover_small | iPod 5G album art small |
| 1029 | 200×200 | 400 | RGB565_LE | cover_large | iPod 5G album art large |
| 1031 | 42×42 | 84 | RGB565_LE | cover_small | Nano album art small |
| 1032 | 42×37 | 84 | RGB565_LE | photo_list | Nano list thumbnail |
| 1036 | 50×41 | 100 | RGB565_LE | photo_list | Video list thumbnail |
| 1044 | 128×128 | 256 | RGB565_LE | cover_medium | Classic album art medium |
| 1055 | 128×128 | 256 | RGB565_LE | cover_medium | Classic album art medium |
| 1056 | 128×128 | 256 | RGB565_LE | cover_medium_alt | 128x128 cover art (alternate) |
| 1060 | 320×320 | 640 | RGB565_LE | cover_large | Classic album art large |
| 1061 | 56×56 | 112 | RGB565_LE | cover_small | Classic album art small |
| 1066 | 64×64 | 128 | RGB565_LE | photo_thumb | Classic photo thumbnail |
| 1067 | 720×480 | 1080 | I420_LE | tv_out | Classic TV output (YUV) |
| 1068 | 128×128 | 256 | RGB565_LE | cover_medium_alt | Classic album art medium (alt 2) |
| 1071 | 240×240 | 480 | RGB565_LE | cover_large | Nano 4G album art large |
| 1073 | 240×240 | 480 | RGB565_LE | cover_large | Nano 5G/6G album art large |
| 1074 | 50×50 | 100 | RGB565_LE | cover_xsmall | Nano album art tiny |
| 1078 | 80×80 | 160 | RGB565_LE | cover_small | Nano 4G/5G album art small |
| 1079 | 80×80 | 160 | RGB565_LE | photo_thumb | Nano 4G/5G photo thumbnail |
| 1081 | 640×480 | 0 | JPEG | photo_full | JPEG photo format (experimental/legacy) |
| 1083 | 240×320 | 480 | RGB565_LE | photo_full | Nano 4G photo full screen (portrait) |
| 1084 | 240×240 | 480 | RGB565_LE | cover_large_alt | Nano 4G album art (alt) |
| 1085 | 88×88 | 176 | RGB565_LE | cover_medium | Nano 6G album art medium |
| 1087 | 384×384 | 768 | RGB565_LE | photo_large | Nano 5G photo large |
| 1089 | 58×58 | 116 | RGB565_LE | cover_small | Nano 6G album art small |
| 1092 | 80×80 | 160 | RGB565_LE | photo_thumb | Nano 6G photo thumbnail |
| 1093 | 512×512 | 1024 | RGB565_LE | photo_full | Nano 6G photo full screen |
| 2002 | 50×50 | 100 | RGB565_BE | cover_small | iPod Mobile cover art small |
| 2003 | 150×150 | 300 | RGB565_BE | cover_large | iPod Mobile cover art large |
| 3001 | 256×256 | 512 | REC_RGB555_LE | cover_large | iPod touch cover art large |
| 3002 | 128×128 | 256 | REC_RGB555_LE | cover_medium | iPod touch cover art medium |
| 3003 | 64×64 | 128 | REC_RGB555_LE | cover_small | iPod touch cover art small |
| 3005 | 320×320 | 640 | RGB555_LE | cover_xlarge | iPod touch cover art xlarge |

Device override sets (also chapter 06, depended on by `artwork_format_candidates`):

```text
CLASSIC_COVER_ART_FORMATS   = (formats 1055, 1060, 1061, 1068)
NANO_7G_COVER_ART_OVERRIDES = (
    format 1010 (global),
    ArtworkFormat(1013, 50,  50, 100, "RGB565_LE", "cover_xsmall",   "Nano 7G album art tiny"),
    ArtworkFormat(1015, 58,  58, 116, "RGB565_LE", "cover_small",    "Nano 7G album art small"),
    ArtworkFormat(1016, 57,  57, 116, "RGB565_LE", "cover_small_alt","Nano 7G album art small (aligned)"),
)
```

`ITHMB_SIZE_MAP: dict[int, ArtworkFormat]` — derived at import: iterate
`ITHMB_FORMAT_MAP.values()` in insertion order; for each with
`row_bytes * height > 0`, map that byte size → format, **first wins** on
collisions. Exported from `podsync.device` and re-exported by
`podsync.artworkdb_writer` (§1) but not otherwise used in this chapter.

### 4.3 Device functions consumed

| Import path | Signature | Contract used here |
|---|---|---|
| `podsync.device.ITHMB_FORMAT_MAP` | `dict[int, ArtworkFormat]` | global format lookup (§4.2) |
| `podsync.device.ITHMB_SIZE_MAP` | `dict[int, ArtworkFormat]` | re-export only |
| `podsync.device.ithmb_formats_for_device` | `(family: str, generation: str, *, capacity=None, model_number=None) -> dict[int, tuple[int,int]]` | `{format_id: (width, height)}` for a device's cover art. Reference values: `("iPod Classic","6th Gen")` → keys `[1055, 1060, 1061, 1068]`; `("iPod","5th Gen")` → `[1028, 1029]` |
| `podsync.device.ArtworkFormat` | dataclass §4.1 | re-exported |
| `podsync.device.artwork_presets.artwork_format_candidates` | `() -> tuple[ArtworkFormat, ...]` | all registry entries + `CLASSIC_COVER_ART_FORMATS` + `NANO_7G_COVER_ART_OVERRIDES`, deduplicated by `(format_id, width, height, pixel_format)` first-wins, order preserved |
| `podsync.device.resolve_cover_art_format_definitions_for_device` | `(device) -> dict[int, ArtworkFormat]` | `{}` when `device is None` |
| `podsync.device.get_current_device_for_path` | `(path) -> DeviceInfo \| None` | `None` when no device identified at path |
| `podsync.device.durability.open_unique_sibling_temp` | `(target, *, mode="w+b") -> (Path, file)` | `tempfile.mkstemp(prefix=".iop-", suffix=".tmp", dir=target.parent)` + `os.fdopen`; exclusive create |
| `podsync.device.durability.flush_written_file` | `(file, *, full=False) -> None` | `flush()` + `os.fsync(fd)` (+ Windows `FlushFileBuffers`) |
| `podsync.device.durability.durable_replace` | `(source, target) -> None` | `os.replace` + parent-dir fsync (POSIX) |
| `podsync.device.durability.durable_unlink` | `(path, *, missing_ok=False) -> None` | `Path.unlink` + parent-dir fsync |
| `podsync.device.path_safety.resolve_device_path` | `(ipod_root, device_relative_path, *, allowed_subtree) -> Path` | resolves within subtree; raises `UnsafeDevicePathError` on escape |
| `podsync.device.write_guard.DeviceWriteSafetyError` | `RuntimeError` subclass | raised by `read_existing_artwork` on malformed DB |

Behavioral expectations the writer relies on (implemented in chapter 06/07):
`get_artwork_format_definitions(ipod_path)` returns `{}` for an unidentified
device (no format guessing); `durable_replace` is atomic (`os.replace`).

---

## 5. `podsync.artworkdb_parser`

### 5.1 Entry point

```text
parse_artworkdb(file) -> dict
```

* `file` is a `str` path (opened `rb` and fully read) **or** any object with
  `.read()` (result used directly). Anything else →
  `TypeError("file must be a path (str) or a file-like object")`.
* Parses the chunk at offset 0 and returns `result.get("result", result)` —
  i.e. the **mhfd dict** directly (unwrapped from the `{"nextOffset","result"}`
  wrapper).

Top-level return shape:

```text
{
  "unk1".."unk3": int, "childCount": int, "next_mhii_id": int,
  "unk4", "unk5": int, "unk6".."unk10": int,
  "mhli": [ image, ... ],   # present when dataset type 1 exists
  "mhla": {},               # dataset type 2 (parsed as empty dict)
  "mhlf": {},               # dataset type 3 (parsed as empty dict)
}
```

Before returning, `parse_mhfd` recursively converts **every `bytes` value**
anywhere in the structure to its base64 ASCII string (JSON friendliness).
Strings produced by MHOD decoding are already `str` and pass through.

### 5.2 `chunk_parser.parse_chunk(data, offset) -> dict`

Reads `tag = data[offset:offset+4].decode("utf-8")` (**strict** UTF-8 — invalid
bytes raise `UnicodeDecodeError`), `header_length` = u32 at `+4`,
`chunk_length` = u32 at `+8`, then dispatches:

| tag | action |
|---|---|
| `mhfd` | `parse_mhfd(data, offset, header_length, chunk_length)` |
| `mhsd` | `parse_mhsd(...)` |
| `mhli` | `parse_mhli(...)` |
| `mhii` | `parse_imageItem(...)` |
| `mhni` | `parse_mhni(...)` |
| `mhod` | `parse_mhod(...)` |
| `mhla`, `mhba`, `mhia`, `mhlf`, `mhif`, `mhaf` | return `{}` (no children parsed) |
| anything else | `raise ValueError(f"Unknown chunk type: {tag}")` |

Imports of the per-tag parsers are done lazily inside the `match` to avoid
circular imports.

### 5.3 Per-parser functions (all in `podsync.artworkdb_parser.<mod>`)

| Function | Module | Returns |
|---|---|---|
| `parse_mhfd(data, offset, header_length, chunk_length)` | `mhfd_parser` | `{"nextOffset": <after last child>, "result": <mhfd dict, base64-cleaned>}` |
| `parse_mhsd(data, offset, header_length, chunk_length)` | `mhsd_parser` | `{"datasetType": u16@+12, "result": <unwrapped child>, "nextOffset": offset + chunk_length}` |
| `parse_mhli(data, offset, header_length, imageCount)` | `mhli_parser` | `{"nextOffset": <after last child>, "result": [image, ...]}` — loops `imageCount` times where the caller passes the **`+8` field** as `imageCount` |
| `parse_imageItem(data, offset, header_length, chunk_length)` | `mhii_parser` | `{"nextOffset": offset + chunk_length, "result": image dict}` (§5.4) |
| `parse_mhod(data, offset, header_length, chunk_length)` | `mhod_parser` | `{"nextOffset": offset + chunk_length, "result": ...}` (§2.9) |
| `parse_mhni(data, offset, header_length, chunk_length)` | `mhni_parser` | `{"nextOffset": offset + chunk_length, "result": mhni dict}` (§2.10) |

`parse_mhfd` child loop: children start at `offset + header_length`; each
iteration uses the child's `nextOffset`, and stores
`datafile[CHUNK_TYPE_MAP[child["datasetType"]]] = child["result"]` — a child
whose dataset type is not 1/2/3 raises `KeyError`.

`parse_imageItem` child loop: children start at `offset + header_length`; each
child must be an mhod. Its result is classified with `mhod_type_name(t)`:

* name known → `image[name] = mhod_result`; additionally, if
  `is_mhod_container(t)` (type ∈ {2,5,6}), append `mhod_result` to
  `image.setdefault("_image_containers", [])`.
* name unknown (type outside 1,2,3,5,6) → append to
  `image.setdefault("_unknown_mhods", [])`.

Resulting image dict keys: `img_id, songId, unk1, rating, unk2,
originalDate, exifTakenDate, srcImgSize` (§2.8) plus the mhod-derived keys
(e.g. `"Thumbnail Image"`).

### 5.4 Worked example of a parsed entry

Round-tripping a file written by this package yields (conceptually):

```text
image = {
  "img_id": 100, "songId": 12345, "unk1": 0, "rating": 0, "unk2": 0,
  "originalDate": 0, "exifTakenDate": 0, "srcImgSize": 45678,
  "Thumbnail Image": {
      "mhodType": 2,
      "Thumbnail Image": {
          "nextOffset": int,
          "result": {
              "correlationID": 1055, "ithmbOffset": 0, "imgSize": 32768,
              "verticalPadding": 0, "horizontalPadding": 0,
              "imageHeight": 128, "imageWidth": 128, "unk1": 0,
              "imgSize2": 32768,
              "estimatedPixmapHeight": 128, "estimatedPixmapWidth": 128,
              "image_format": {"height", "width", "format", "description", "format_id"},
              3: {"mhodType": 3, "File Name": ":F1055_1.ithmb"},
          },
      },
  },
}
```

### 5.5 Parser exceptions

| Exception | Condition |
|---|---|
| `TypeError` | `parse_artworkdb` got neither `str` nor file-like |
| `ValueError("Unknown chunk type: …")` | unregistered four-byte tag |
| `UnicodeDecodeError` | tag bytes not valid UTF-8 (strict decode) |
| `struct.error` / `IndexError`-equivalents | truncated buffers are **not** caught by the generic parser — it assumes well-formed input (the strict validator is `read_existing_artwork`, §7.4) |

### 5.6 `podsync.artworkdb_parser.constants`

Compatibility aliases (same objects as the shared module):
`chunk_type_map = CHUNK_TYPE_MAP`,
`identifier_readable_map = IDENTIFIER_READABLE_MAP`,
`mhod_type_map = MHOD_TYPE_MAP`.

---

## 6. `podsync.artworkdb_shared` API

### 6.1 `binary`

| Name | Signature / value | Contract |
|---|---|---|
| `GENERIC_CHUNK_HEADER_SIZE` | `12` | |
| `MIN_TYPED_CHUNK_HEADER_SIZE` | `14` | |
| `ChunkHeader` | frozen dataclass `(tag: str, header_size: int, length_or_count: int)` | |
| `read_chunk_header(data, offset) -> ChunkHeader` | | Raises `ValueError(f"ArtworkDB chunk header outside buffer at offset {offset}")` if `offset < 0` or `offset + 12 > len(data)`. Tag decoded UTF-8 with `errors="replace"` (lenient, unlike `parse_chunk`) |
| `chunk_fits(data, offset, total_size, min_header_size=12) -> bool` | | `offset >= 0 and total_size >= min_header_size and offset + total_size <= len(data)` |
| `total_length_is_valid(data, offset, header_size, total_size, min_header_size=12, end=None) -> bool` | | boundary = `len(data)` if `end is None` else `min(len(data), end)`; true iff `offset >= 0 and header_size >= min_header_size and total_size >= header_size and offset + total_size <= boundary` |
| `read_u16 / read_i16 / read_u32 / read_u64(data, offset)` | | `struct.unpack_from` with `<H`, `<h`, `<I`, `<Q` |

### 6.2 `mhod` helpers

| Function | Contract |
|---|---|
| `mhod_type_info(mhod_type) -> dict[str,str] \| None` | `MHOD_TYPE_MAP.get(ArtworkMhodType(t))`; non-enum value → `None` |
| `is_mhod_container(mhod_type) -> bool` | info exists and `info["type"] == "Container"` |
| `mhod_type_name(mhod_type) -> str \| None` | `info["name"]` or `None` |
| `mhod_string_encoding(mhod_type) -> (str,int)` | `(“utf-16-le”, 2)` for type 3 else `(“utf-8”, 1)` |
| `encode_mhod_string_body(mhod_type, value) -> bytes` | body per §2.12 |
| `decode_mhod_string_body(data, body_offset, body_end) -> str \| None` | §2.12 |
| `decode_mhod_string_chunk(data, offset, total_size) -> str \| None` | §2.12 |

### 6.3 `mhni` helpers

| Name | Contract |
|---|---|
| `MhniFields` | frozen dataclass with fields per §2.10 + properties `estimated_pixmap_height/width` |
| `read_mhni_fields(data, offset) -> MhniFields` | field table §2.10 |
| `default_stride_pixels_for_format(fmt, width) -> int` | `fmt is None` → `width`. Pixel formats in {`RGB565_LE`, `RGB565_BE`, `RGB565_BE_90`, `RGB555_LE`, `RGB555_BE`, `UYVY`} or starting with `REC_RGB555` → `max(width, row_bytes // 2 if row_bytes else width)`. Otherwise → `width` |
| `expected_size_for_format(fmt, width=None, height=None, stride_pixels=None) -> int` | `fmt is None` → `0`. visible dims default from `fmt.width/height`. stride = `stride_pixels` if given else `default_stride_pixels_for_format(fmt, visible_w)`. 16-bit formats (set above + any `REC_RGB555*`) → `stride * visible_h * 2`; `I420_LE` → `(w & ~1) * (h & ~1) * 3 // 2` (even w/h); `JPEG` → `0`; any other format string → `stride * visible_h * 2` |
| `expected_size_bytes(format_id, width, height, stride_pixels=None, fmt_override=None) -> int` | resolves `fmt = fmt_override if fmt_override is not None else ITHMB_FORMAT_MAP.get(format_id)` then `expected_size_for_format` |
| `infer_image_format(fields) -> dict \| None` | §6.4 |

### 6.4 `infer_image_format` (MHNI → format dict)

Goal: identify the `ArtworkFormat` for an MHNI observed on disk.

Compatibility predicate `_candidate_is_compatible(fmt, fields) -> (ok, size_delta, dim_delta)`:

* `expected = expected_size_for_format(fmt)` (using `fmt`'s own dims).
* `corr_exact = expected > 0 and expected == fields.image_size`.
* `corr_close`: with `est_w/est_h` = estimated pixmap dims, true iff
  `est_w > 0 and est_h > 0 and` (`|est_w - fmt.width| ≤ 2 and |est_h - fmt.height| ≤ 2`
  **or** the same with width/height swapped (rotation)).
* If `expected == 0 and corr_close` → treat as exact (`corr_exact = True`).
* `size_delta = |image_size − expected|` if `expected > 0` else `0`;
  `dim_delta = |est_w − fmt.width| + |est_h − fmt.height|`.
* `ok = corr_exact or corr_close`.

Selection, in order:

1. **Same-id candidates**: filter `artwork_format_candidates()` to
   `candidate.format_id == fields.format_id`; among *compatible* ones pick the
   minimum `score = size_delta + dim_delta` (strict `<` keeps the earliest on
   ties). If found → result dict (step 4 shape, no `score`).
2. **Global map**: `ITHMB_FORMAT_MAP.get(fields.format_id)`; if present and
   compatible → result dict (no `score`).
3. **Best-effort over all candidates**: score each candidate:
   `dim_diff = |est_h − candidate.height| + |est_w − candidate.width|`;
   if `expected_size_for_format(candidate) > 0` add
   `size_delta / max(1, candidate.row_bytes, candidate.width)` to the score,
   else score = `dim_diff`. Take the minimum (earliest wins ties) and return it
   **with** `"score"` included. Here `format_id` is the *candidate's* id; in
   steps 1–2 it is the MHNI's `format_id`.
4. Result dict shape: `{"height", "width", "format" (=pixel_format),
   "description", "format_id"}` (+ optional `"score"`).
   No candidates at all (cannot happen with a populated registry) → `None`.

### 6.5 `mhlf`

`extract_format_ids(data: bytes) -> list[int]` — tolerant scan for MHIF
correlation ids:

1. `len(data) < 32` or `data[:4] != b"mhfd"` → `[]`.
2. `child_count` = u32 at 20; `offset` = mhfd `header_size`.
3. Per child: require `offset + 14 ≤ len(data)` and tag `mhsd`, else stop;
   read `mhsd` header/total, `ds_type` = u16 at +12; validate with
   `total_length_is_valid(..., min_header_size=14)` else stop.
4. If `ds_type == 3` (FILE_LIST): within `[offset, offset+mhsd_total)`, the
   child at `offset + mhsd_header` must be `mhlf` (needs 12 bytes); iterate
   `mhlf.length_or_count` MHIF entries starting at `mhlf_offset + mhlf_header`:
   each must have ≥ 20 bytes left in the dataset, tag `mhif`,
   `mhif_size = u32 at +4 ≥ 20` and inside the dataset, else break; append
   `u32 at mhif_offset + 16` (the correlation id); advance by `mhif_size`.
5. Advance `offset += mhsd_total` and continue. Any structural mismatch stops
   the scan early (returns what was collected).

---

## 7. `podsync.artworkdb_writer.artworkdb_chunks`

### 7.1 Types used (defined in §8 `artwork_types`, referenced here)

`IthmbLocation(filename, offset)`, `ArtworkEntry`, `ArtworkFormatPayload` =
`EncodedFormatPayload | PassthroughFormatRef`.

Alias: `IthmbLocationInput = IthmbLocation | tuple[str, int] | int | None`.
`_coerce_ithmb_location(fmt_id, location)`:

* `IthmbLocation` → copy with normalized filename;
* tuple `(filename, offset)` → normalized filename + int offset;
* `int` or `None` → `IthmbLocation(F{fmt}_1.ithmb, int(location or 0))`.

### 7.2 Serialization functions (private, but they define the bytes)

| Function | Bytes produced |
|---|---|
| `_write_mhod_string(mhod_type, string)` | 24-byte mhod header (`+4=24`, `+8=24+len(body)`, `+12=u16 type`) + `encode_mhod_string_body` (§2.12) |
| `_write_mhni(format_id, location, payload)` | 76-byte mhni header per §2.10 (`+12=1`, `+16=format_id`, `+20=offset`, `+24=payload.size`, `+28=i16 vpad=max(0,payload.vpad)`, `+30=i16 hpad=max(0,payload.hpad)`, `+32=u16 height`, `+34=u16 width`, `+40=payload.size`) + child `_write_mhod_string(3, ":" + normalized_filename)`. `stride = max(width, payload.stride_pixels)` is only used for a debug size-mismatch log (`logger.debug("ART: MHNI size mismatch for fmt …")`) when both pads are 0 and `expected_size_bytes(...) > 0 != payload.size`. **Raises `ValueError(f"MHNI padding too large for format {format_id}: vpad=… hpad=…")` if vpad or hpad > 0x7FFF** (after clamping negatives to 0) |
| `_write_mhod_container(mhod_type, mhni_bytes)` | 24-byte mhod header (`+8 = 24 + len(mhni)`, `+12 = type`) + mhni bytes |
| `_write_mhii(entry, format_locations)` | 152-byte mhii per §2.8, `+12` = number of children; children = one `_write_mhod_container(2, mhni)` **per format, sorted by format id ascending**; location taken from `format_locations.get(fmt_id, 0)` through `_coerce_ithmb_location` |
| `_write_mhli(entries, format_locations_map)` | 92-byte header: tag, `+4 = 92`, `+8 = len(entries)` (count!) + concatenated mhii |
| `_write_mhla()` | 92-byte header, `+4 = 92`, `+8 = 0` |
| `_write_mhif(format_id, image_size)` | 124-byte header: `+4 = 124`, `+8 = 124`, `+16 = format_id`, `+20 = image_size`; rest zero |
| `_write_mhlf(format_ids, image_sizes)` | 92-byte header (`+8 = len(format_ids)` count) + mhif per id (caller's list order) |
| `_write_mhsd(ds_type, child)` | 96-byte header: `+8 = 96 + len(child)`, `+12 = u16 ds_type` + child |
| `_write_mhfd(datasets, next_mhii_id, reference_mhfd=None)` | 132-byte header per §2.4: `+8 = total`, `+16 = 2`, `+20 = len(datasets)`, `+28 = next_mhii_id`; if `reference_mhfd` is non-empty and `len ≥ 48` copy bytes `[32:48]` from it, then set `+48 = 2`, then if `len ≥ 68` copy bytes `[60:68]`; + concatenated datasets |

### 7.3 `build_artworkdb`

```text
build_artworkdb(entries: list[ArtworkEntry],
                 format_locations_map: Mapping[int, Mapping[int, IthmbLocationInput]],
                 format_ids: list[int],
                 image_sizes: dict[int, int],
                 next_mhii_id: int,
                 reference_mhfd: bytes | None = None) -> bytes
```

Assembles exactly three datasets in this order and wraps them in one mhfd:

1. `mhsd(1)` → `mhli(entries)`; for entry `e`, locations =
   `format_locations_map[e.img_id]`.
2. `mhsd(2)` → empty `mhla`.
3. `mhsd(3)` → `mhlf(format_ids, image_sizes)` (mhif order = `format_ids`
   order).

`image_sizes[fmt]` is one u32 per format for MHIF (caller supplies the mode
size, §12.9 step 13). `next_mhii_id` lands at mhfd `+28`.

### 7.4 `read_existing_artwork`

```text
read_existing_artwork(artworkdb_path: str, artwork_dir: str) -> dict[int, dict]
```

Strict reader for the writer's safety gate. Behavior:

* `FileNotFoundError` → `{}` (no existing DB is fine).
* Any other `OSError` → `DeviceWriteSafetyError` with message
  `"The existing ArtworkDB could not be read safely. podsync stopped before replacing artwork metadata: {exc}"`.
* Structural failures raise `DeviceWriteSafetyError` built by a helper whose
  message template is
  `"The existing ArtworkDB is malformed or truncated. podsync stopped before replacing artwork metadata ({detail})."`
  (the contract substring is **`ArtworkDB is malformed or truncated`**).

Traversal rules (all sizes/offsets validated with `total_length_is_valid` or
explicit bounds; `detail` strings below are the required texts):

| Step | Validation / detail on failure |
|---|---|
| File head | `len ≥ 32` and `data[:4] == b"mhfd"` else `missing or truncated mhfd header` |
| mhfd header | `read_chunk_header(0)` (ValueError/struct.error → detail = exception text); `total_length_is_valid(data, 0, header_size, total, min_header_size=32)` else `invalid mhfd size header={header_size} total={total}`; `child_count` = u32@20; `database_end = total` |
| Each mhsd child | need `offset + 14 ≤ database_end` and tag `mhsd` else `missing mhsd child {i} at offset {offset}`; `ds_type` = u16@+12; `total_length_is_valid(..., min_header_size=14, end=database_end)` else `invalid mhsd chunk at offset {offset}` |
| ds_type == 1 | at `offset + mhsd_header` require tag `mhli` within `dataset_end = offset + mhsd_total` else `missing mhli image list at offset …`; mhli `header_size ≥ 12` and inside dataset else `invalid mhli chunk at offset …`; iterate `mhli.length_or_count` (=mhii count) entries from `mhli_offset + mhli_header` |
| Each mhii | need `mhii_offset + 52 ≤ dataset_end` and tag `mhii` else `missing mhii entry {i} at offset …`; `total = u32@+8 ≥ 52` and inside dataset else `invalid mhii chunk at offset …` |
| mhii header | ≥ 52 bytes in chunk; `header_size = u32@+4` in `[52, total]` else `invalid mhii header size {n} at offset …`; read `child_count@12`, `img_id@16`, `song_id u64@20`, `src_img_size@48` |
| Each mhod child | need `+14` bytes and tag `mhod` else `missing mhod child {i} at offset …`; header/total valid with `min_header_size=14` inside `entry_end` else `invalid mhod chunk at offset …`; advance by `mhod_total` |
| type == 2 (thumbnail) | at `child_offset + mhod_header` require ≥ 76 bytes and tag `mhni` within `child_offset + mhod_total` else `invalid mhni thumbnail at offset …`; read fields (§2.10); filename via `_parse_mhni_filename` then normalized; `ithmb_path = ithmb_path_for_filename(artwork_dir, format_id, filename)`; **include ref only if `os.path.exists(ithmb_path)` and `img_size > 0`** |
| Other mhod types | skipped (advance only) |

`_parse_mhni_filename(data, mhni_offset, container_end) -> str | None`:
walks mhod children of the mhni starting at `mhni_offset + max(header_size_read, 76)`,
bounded by `mhni_end = min(container_end, mhni_offset + mhni_total)`; requires
≥ 24 bytes per child, tag `mhod`, `header_size ≥ 24`, `total ≥ header`, child
fully inside `mhni_end` — any violation breaks the walk (`None`); returns the
decoded string of the first type-3 mhod (`decode_mhod_string_chunk`).

Result entry shape (keyed by `img_id`):

```text
{img_id: {"img_id": int, "song_id": int, "src_img_size": int,
          "formats": {format_id: ExistingFormatRef}}}
```

MHII entries that end up with **no** formats are dropped (return `None`).
`ExistingFormatRef` construction: `path`=resolved ithmb path,
`ithmb_offset`, `size=img_size`, `width=max(1, image_width)`,
`height=max(1, image_height)`, `hpad=max(0, horizontal_padding)`,
`vpad=max(0, vertical_padding)`, `ithmb_filename`=normalized name.

If a child dataset tag check fails at the top level (a non-`mhsd` child), the
loop raises `missing mhsd child …` — i.e. any deviation from the tree in §2.3
is a safety error, never a partial read.

---

## 8. `podsync.artworkdb_writer.artwork_types`

All frozen unless noted.

| Class | Fields | Notes |
|---|---|---|
| `IthmbLocation` | `filename: str`, `offset: int` | frame position inside one ithmb file |
| `ExistingFormatRef` | `path, ithmb_offset, size, width, height, hpad=0, vpad=0, ithmb_filename=""` | properties: `stride_pixels = max(1, width + hpad)`, `stored_height = max(1, height + vpad)` |
| `EncodedFormatPayload` | `data: bytes, width, height, size, stride_pixels, hpad=0, vpad=0, pixel_format: str \| None = None` | classmethod `from_existing_ref(ref, data)` copies `width, height, size, hpad, vpad` and `stride_pixels=ref.stride_pixels`; only `pixel_format` stays `None` |
| `PassthroughFormatRef` | same fields as `ExistingFormatRef` | classmethod `from_existing_ref(ref)`; property `stride_pixels = max(1, width + hpad)` (no `stored_height`) |
| `ArtworkFormatPayload` | type alias | `EncodedFormatPayload \| PassthroughFormatRef` |
| `ArtworkPayload` (mutable) | `formats: dict[int, ArtworkFormatPayload] = {}`, `src_img_size: int = 0` | one unique asset's payloads |
| `ArtworkEntry` (mutable) | `img_id: int, db_track_id: int, art_hash: str \| None, src_img_size: int, formats: dict = {}, db_track_ids: list = []` | one MHII record |

---

## 9. `podsync.artworkdb_writer.ithmb_codecs`

Module-level imports allowed: `io`, `logging`, `PIL.Image`, plus
`podsync.device.ITHMB_FORMAT_MAP` and shared mhni helpers. **No numpy** —
implement the arithmetic below with Pillow (`Image.frombytes`, `Image.tobytes`,
point ops, `resize`) and `struct`/`bytes`.

### 9.1 Format resolution helpers

| Function | Contract |
|---|---|
| `format_pixel_format(format_id, fmt_override=None) -> str` | `fmt_override.pixel_format` if override else `ITHMB_FORMAT_MAP[format_id].pixel_format` if known else `"UNKNOWN"` |
| `format_dimensions(format_id, fallback_w, fallback_h, fmt_override=None) -> (int, int)` | known fmt → `(fmt.width, fmt.height)`; unknown → fallbacks |
| `default_stride_pixels(format_id, width, fmt_override=None) -> int` | §6.3 rule |
| `expected_size_bytes(format_id, width, height, stride_pixels=None, fmt_override=None) -> int` | delegates to shared §6.3 |

### 9.2 Packing math (shared by encode + decode)

RGB888 → 16-bit packings (per pixel, from 8-bit channels):

* **RGB565**: `v = ((R >> 3) << 11) | ((G >> 2) << 5) | (B >> 3)`
* **RGB555**: `v = ((R >> 3) << 10) | ((G >> 3) << 5) | (B >> 3)`

16-bit → RGB888 expansion:

* **RGB565**: `r5=(v>>11)&0x1F; g6=(v>>5)&0x3F; b5=v&0x1F`;
  `R=(r5<<3)|(r5>>2); G=(g6<<2)|(g6>>4); B=(b5<<3)|(b5>>2)`
* **RGB555**: `r5=(v>>10)&0x1F; g5=(v>>5)&0x1F; b5=v&0x1F`;
  channel = `(x << 3) | (x >> 2)`

Row padding (`_pad_packed_rows` semantics): given a `width × height` array of
u16 and `stride = max(1, stride_pixels)`, if `stride > width` each row becomes
`stride` pixels with the tail zero-filled; if `stride ≤ width` data is
unchanged. Encoded output = each row serialized as `stride` u16 values in the
codec's byte order → size `stride * height * 2` bytes.

Byte order suffixes: `_LE` and any `REC_RGB555_LE` → little-endian (`<u2`);
`RGB565_BE`, `RGB565_BE_90`, `RGB555_BE` → big-endian (`>u2`).

### 9.3 `encode_image_for_format`

```text
encode_image_for_format(source_img: Image.Image, format_id: int,
                        target_width: int | None = None,
                        target_height: int | None = None,
                        fmt_override=None) -> EncodedFormatPayload
```

1. `pf = format_pixel_format(...)`.
2. `w, h = format_dimensions(format_id, target_width or source.width,
   target_height or source.height, fmt_override)` — **if the format is known,
   the registry/override dimensions win; targets are fallbacks only.**
3. `stride = default_stride_pixels(format_id, w, fmt_override)`.
4. `base = source.convert("RGB").resize((w, h), Image.Resampling.LANCZOS)`
   — direct resize, **aspect ratio is not preserved**.

Per-format encoding (all rows padded to `stride` unless noted):

| `pixel_format` | Steps | Output |
|---|---|---|
| `RGB565_LE` (also the **default** fallthrough for any format string not listed below, as long as it is not `"UNKNOWN"`) | pack 565 from `base`, pad rows to stride, serialize LE | `size = stride*h*2`, `pixel_format="RGB565_LE"` |
| `RGB565_BE` | pack 565, pad, serialize BE | same size, `"RGB565_BE"` |
| `RGB565_BE_90` | `rotated = base.transpose(Image.Transpose.ROTATE_270)`, pack 565 from rotated, pad to stride, serialize BE; payload dims stay `(w, h)` | same size |
| `RGB555_BE` | pack 555, pad, serialize BE | same size |
| `RGB555_LE`, `REC_RGB555_LE` | pack 555, pad, serialize LE | same size |
| `JPEG` | save `base` as JPEG, `quality=92, optimize=False`; **no row padding**; `stride_pixels` field still = `stride` | `size = len(jpeg bytes)` |
| `UYVY` | force `w` even (`w -= 1` and re-resize the already resized `base` with LANCZOS if odd). Per-pixel float: `y = clip(0.257R + 0.504G + 0.098B + 16, 0, 255)` → u8; `u = clip(-0.148R - 0.291G + 0.439B + 128, 0, 255)`; `v = clip(0.439R - 0.368G - 0.071B + 128, 0, 255)` (u/v kept fractional). `u2[c] = (u[2c] + u[2c+1]) / 2`, `v2` likewise, truncated to u8. Row layout (row = `w*2` bytes): byte classes at `0::4 = u2`, `1::4 = y[0::2]`, `2::4 = v2`, `3::4 = y[1::2]` (macropixel `U Y0 V Y1`, `w/2` macropixels) | `size = w*h*2`, `"UYVY"` |
| `I420_LE` | force `w,h` even (`& ~1`, re-resize if changed). Same y/u/v formulas as UYVY. `u420` = mean of each 2×2 u block (truncated to u8), `v420` likewise. Planar output: Y plane `w*h` bytes, then U plane `(w/2)*(h/2)`, then V plane | `size = w*h*3//2`, `"I420_LE"` |
| `"UNKNOWN"` | **raises `ValueError(f"Unsupported unknown pixel format for format_id={format_id}")`** | — |

Returned `EncodedFormatPayload`: `data`, `width=w`, `height=h`,
`size=len(data)`, `stride_pixels=stride`, `pixel_format=pf` (for the default
branch the field is `"RGB565_LE"`), `hpad=0`, `vpad=0`.

Truncation of clipped floats: the conversion is defined in single precision;
double precision is acceptable, always **truncating toward zero** after
clipping. Packed RGB
outputs must be bit-identical; UYVY/I420 outputs are specified with a ±1 LSB
tolerance per channel (see §9.9).

### 9.4 `decode_pixels_for_format`

```text
decode_pixels_for_format(format_id, pixel_bytes, width, height,
                         hpad=0, vpad=0, fmt_override=None) -> Image.Image | None
```

Pre-clamps: `width = max(1, int(width))`, `height = max(1, ...)`,
`hpad = vpad = max(0, ...)`.

Common flow for 16-bit formats: infer stored geometry (§9.5); if
`(stored_w, stored_h) == (0, 0)` → `None`; `needed = stored_w*stored_h*2`; if
`len(pixel_bytes) < needed` → `None`; if there are **more** bytes than needed
and excess `> 0.1 * needed`, log a warning containing
`"extra bytes"` and `"truncating"`; decode the first `needed` bytes as u16
(LE/BE per format) reshaped row-major `(stored_h, stored_w)`, expand to RGB,
apply format-specific rotation, then crop (§9.7), then return an RGB PIL image.

| pf group | dtype | expansion | extras |
|---|---|---|---|
| `RGB565_LE` | `<u2` | 565 | — |
| `RGB565_BE`, `RGB565_BE_90` | `>u2` | 565 | `RGB565_BE_90`: after expansion, rotate the RGB raster **90° counter-clockwise** (the pixel at row r, column c moves to row `W−1−c`, column r), *then* crop |
| `RGB555_LE`, `REC_RGB555_LE` | `<u2` | 555 | — |
| `RGB555_BE` | `>u2` | 555 | — |
| `UYVY` | bytes | see below | geometry inference with `require_even_width=True`; crop; then `_fix_1019_layout` when `format_id == 1019` |
| `I420_LE` | bytes | see below | **no crop, no 1019 fix** |
| `JPEG` | — | `Image.open(BytesIO(pixel_bytes)).convert("RGB")`; **any** exception → `None` | — |
| `"UNKNOWN"` / anything else | — | falls through → `None` | — |

**UYVY decode**: row = `stored_w*2` bytes. Per row: `u = row[0::4]`,
`y0 = row[1::4]`, `v = row[2::4]`, `y1 = row[3::4]` (each `stored_w/2`
values). Y raster: even columns ← `y0`, odd columns ← `y1`. U/V rasters:
each value duplicated twice horizontally (`stored_w` values). Inverse
BT.601 with `c = y-16`, `d = u-128`, `e = v-128`:

```
r = clip((298.082*c + 408.583*e) / 256, 0, 255)
g = clip((298.082*c - 100.291*d - 208.120*e) / 256, 0, 255)
b = clip((298.082*c + 516.412*d) / 256, 0, 255)
```

then truncate to u8.

**I420 decode**: `width &= ~1`, `height &= ~1`;
`y_size = w*h`, `uv_size = (w/2)*(h/2)`, `needed = y_size + 2*uv_size`; short
→ `None`; excess `> 10%` → debug log (same keywords, level **debug**).
Planes: Y `(h, w)`, U/V `(h/2, w/2)` as floats; upsample U/V by replicating
each sample 2× on both axes; same inverse BT.601 as above; **return without
crop or 1019 repair**.

### 9.5 Stored-geometry inference (`_resolve_packed_geometry`)

Purpose: given `pixel_bytes` of a packed 16-bit payload plus visible
`(width, height)` and MHNI pads `(hpad, vpad)`, find `(stored_w, stored_h)`
with `stored_w * stored_h == len(pixel_bytes) // 2`.

Candidate generation (in this order; each candidate is `(sw, sh)` with
`sw, sh > 0`, `sw*sh == px_count`, optionally `sw` even when
`require_even_width`; **deduplication is first-wins** — a `(sw, sh)` already
added keeps the priority it got at first insertion):

| # | Rule | Priority |
|---|---|---|
| 1 | `(vis_w + pad_w, vis_h + pad_h)` | 0 |
| 2 | `(vis_w, vis_h)` | 1 |
| 3 | all `(base + dw, base + dh)` with `dw, dh ∈ [-2, +2]` around `(vis_w + pad_w, vis_h + pad_h)` | `2 + abs(dw) + abs(dh)` |
| 4 | same ±2 neighborhood around `(vis_w, vis_h)` | `4 + abs(dw) + abs(dh)` |
| 5 | if `px_count % (vis_h + pad_h) == 0`: `(px_count // (vis_h+pad_h), vis_h+pad_h)` | 2 |
| 6 | if `px_count % vis_h == 0`: `(px_count // vis_h, vis_h)` | 3 |
| 7 | if `px_count % (vis_w + pad_w) == 0`: `(vis_w+pad_w, px_count // (vis_w+pad_w))` | 4 |
| 8 | if `px_count % vis_w == 0`: `(vis_w, px_count // vis_w)` | 5 |

If a format definition resolves (`fmt` from `fmt_override`/`ITHMB_FORMAT_MAP`;
`fmt_w`, `fmt_h`, `stride = row_bytes // 2 if row_bytes > 0 else fmt_w`):

| # | Rule | Priority |
|---|---|---|
| 9 | `(stride, fmt_h)` and `(fmt_w, fmt_h)` | 6 |
| 10 | if `px_count % stride == 0`: `(stride, px_count // stride)` | 7 |
| 11 | if `px_count % fmt_w == 0`: `(fmt_w, px_count // fmt_w)` | 8 |
| 12 | if `px_count % fmt_h == 0`: `(px_count // fmt_h, fmt_h)` | 9 |
| 13 | **row-alignment**: for `alignment` in `(2, 4, 8, 16)`: `aligned_w = ceil(vis_w / alignment) * alignment`; for `bpp` in `(2, 4)`: if `aligned_w * vis_h * bpp == len(pixel_bytes)` add `(aligned_w, vis_h)` prio 1; if `aligned_w * fmt_h * bpp == len` add `(aligned_w, fmt_h)` prio 1 | 1 |

Every candidate additionally carries sort keys
`overflow = max(0, vis_w - sw) + max(0, vis_h - sh)` and
`pad_delta = abs((sw - vis_w) - pad_w) + abs((sh - vis_h) - pad_h)`.
Selection: **minimum of the tuple `(priority, overflow, pad_delta, sw, sh)`**.

If *no* candidate matched: fallback — when `fmt` is known,
`fmt.row_bytes > 0`, `len(pixel_bytes) > row_bytes * fmt.height` and the
excess is `≤ 256` bytes, re-run the same algorithm on the trimmed prefix
`pixel_bytes[:row_bytes * fmt.height]` **with format lookup disabled**; if
that returns a result, use it; otherwise return `(0, 0)`.

`vis_w = max(1, width)`, `vis_h = max(1, height)`, `pad_w = max(0, hpad)`,
`pad_h = max(0, vpad)`; `px_count = len(pixel_bytes) // 2` (≤ 0 → `(0, 0)`).

### 9.6 Format-1019 UYVY repair (`_fix_1019_layout`)

Runs only for `format_id == 1019`, only in the UYVY decode path, after crop,
and only when `h ≥ 120` and `h` is even. Helpers (all on `uint8` RGB rasters,
diffs computed in signed 16-bit):

* `_half_similarity(rgb) -> (mad, p95)`: if `h < 4` or odd → `(999.0, 999.0)`;
  else `top = rgb[:h/2]`, `bottom = rgb[h/2:]`, `diff = |top - bottom|`;
  return `(mean(diff), 95th percentile(diff))` — the percentile over all
  elements (all rows, columns and channels) with **linear interpolation**
  between closest ranks (rank = `0.95 * (n - 1)`).
* `_detail_score(rgb)`: if `h < 2` or `w < 2` → `0.0`; else
  `var(diff along columns) + var(diff along rows)` where `var` is the
  **population** variance (mean of squared deviations).
* `_line_discontinuity_ratio(rgb)`: if `h < 3` → `1.0`; `avg_adj =
  mean(|row[i] - row[i-1]|)` over all adjacent row pairs (all channels);
  if `avg_adj ≤ 0` → `1.0`; `seam_jump = mean(|row[h/2 - 1] - row[h/2]|)`;
  return `seam_jump / avg_adj`.
* `_weave_fields(top, bottom, swap=False)`: output height `2 * len(top)`;
  even rows = top (bottom if `swap`), odd rows = the other half.

Fast path: if `mad < 8.0 and p95 < 30.0` (halves are near-identical) → choose
the half with the larger `_detail_score` (top wins ties) and bilinearly
upscale it to `(w, h)`.

Otherwise score five candidates — `(a)` identity, `(b)` weave no-swap,
`(c)` weave swap, `(d)` top half upscaled to `(h, w)`, `(e)` bottom half
upscaled — by the tuple
`(stacked_penalty, line_discontinuity_ratio, -detail_score)` where
`stacked_penalty = 1.0 if (mad < 8.0 and p95 < 30.0) else 0.0`
computed **on that candidate**; pick the minimum (first wins ties).
Upscales use `Image.Resampling.BILINEAR`.

### 9.7 Visible-region crop (`_crop_visible_region`)

Input: decoded RGB raster `(stored_h, stored_w)` plus visible `(width,
height)` and pads.

1. `visible_w = clamp(width, 1, stored_w)`, `visible_h = clamp(height, 1,
   stored_h)`; `crop_x = crop_y = 0`.
2. Resolve `fmt` (override first, then `ITHMB_FORMAT_MAP`). Only if
   `fmt is not None`, `fmt.role` starts with `"photo"`, and `hpad > 0 or vpad > 0`:
   * `match_w = |stored_w - (width + hpad)| ≤ 2`, `match_h = |stored_h - (height + vpad)| ≤ 2`;
   * if `match_w or match_h` (photo formats store pad centered on both sides):
     `crop_x = clamp(hpad, 0, stored_w - 1)`, `crop_y = clamp(vpad, 0, stored_h - 1)`;
     `padded_w = width - hpad`; if `padded_w > 0` →
     `visible_w = min(stored_w - crop_x, padded_w)` else
     `visible_w = min(stored_w - crop_x, visible_w)`; symmetric for height.
3. Return `rgb[crop_y : max(crop_y+1, crop_y+visible_h),
   crop_x : max(crop_x+1, crop_x+visible_w)]`.

All non-photo formats (any `role` not starting with `"photo"`, or zero pads)
use a **top-left anchored** crop of `min(stored, visible)` size.

### 9.8 Excess-padding logging

Every decode branch that truncates trailing bytes logs (warn unless noted)
with the format name, stored dims, extra byte count and percentage, and the
word `"truncating"`; threshold `excess > needed * 0.1`. I420 uses level
**debug**. Message templates:

* `"RGB565 {w}x{h}: payload has {n} extra bytes ({pct}% padding), truncating"`
* same with `RGB555`, `UYVY`; I420 variant `I420_LE {w}x{h}: …` (even-rounded
  visible dims) at debug.
* `{pct}` is formatted with one decimal (`excess / needed * 100`, `.1f`).

### 9.9 Exactness note

For `RGB565*`, `RGB555*`, `REC_RGB555_LE` encode and decode the output must be
**bit-exact** (integer math only). `UYVY`/`I420` go through floating-point
color conversion; match the formulas above with the given constants, truncate
after clipping; a difference of ±1 LSB per channel against single-precision
evaluation is acceptable. `JPEG` must use `quality=92, optimize=False` for determinism.

---

## 10. `podsync.artworkdb_writer.rgb565`

### 10.1 Derived format tables (module constants, computed at import)

| Name | Value |
|---|---|
| `ALL_KNOWN_FORMATS` | `{fid: (fmt.width, fmt.height) for fid, fmt in ITHMB_FORMAT_MAP.items()}` |
| `IPOD_CLASSIC_FORMATS` | `ithmb_formats_for_device("iPod Classic", "6th Gen")` → keys `[1055, 1060, 1061, 1068]` |
| `IPOD_NANO_1G2G_FORMATS` | `ithmb_formats_for_device("iPod Nano", "1st Gen")` |
| `IPOD_4G_PHOTO_FORMATS` | `ithmb_formats_for_device("iPod", "4th Gen (photo)")` |
| `IPOD_5G_FORMATS` | `ithmb_formats_for_device("iPod", "5th Gen")` → keys `[1028, 1029]` |
| `IPOD_NANO_4G_FORMATS` | `ithmb_formats_for_device("iPod Nano", "4th Gen")` |
| `IPOD_NANO_5G_FORMATS` | `ithmb_formats_for_device("iPod Nano", "5th Gen")` |
| `IPOD_STRIDE_OVERRIDE` | `{}` (empty dict; stride = format width unless a registry `row_bytes` says otherwise) |

Because these are import-time computations, `podsync.device` must expose
`ithmb_formats_for_device` at import time (chapter 06).

### 10.2 Functions

| Signature | Contract |
|---|---|
| `get_artwork_format_definitions(ipod_path: str) -> dict[int, ArtworkFormat]` | `device = get_current_device_for_path(ipod_path)` (lazy import from `podsync.device`); `resolve_cover_art_format_definitions_for_device(device)`; `None` device → `{}` (resolve returns `{}`). **Never guesses another model's formats.** |
| `get_artwork_formats(ipod_path: str) -> dict[int, tuple[int, int]]` | defs = `get_artwork_format_definitions(...)`; if non-empty → `{fid: (fmt.width, fmt.height)}` (logs `INFO "ART: using resolved format definitions: %s"`); if empty → logs `WARNING "ART: no artwork definitions available for device {family} {gen} at {path}; refusing to guess an unrelated device format"` and returns `{}` |
| `image_from_bytes(art_bytes: bytes, *, source_path: str = "") -> Image \| None` | `Image.open(BytesIO(...))`; if mode ≠ `RGB` → `convert('RGBA').convert('RGB')`. `Image.DecompressionBombError` → `ValueError` with message `f"Artwork image exceeds Pillow safety limit: {exc}"` plus, when `source_path` is non-empty, a suffix `" Offending image: {source_path}"` (original exception as `__cause__`). Any other exception → `None` |
| `resize_for_format(img, format_id) -> Image` | `ValueError(f"Unknown format ID: {format_id}")` if `format_id not in ALL_KNOWN_FORMATS`; else `img.resize((w, h), Image.Resampling.LANCZOS)` to the exact format box (no aspect preservation) |
| `rgb888_to_rgb565(img, format_width, format_height, stride=None) -> bytes` | `stride = stride or format_width`; **asserts** image is exactly `format_width × format_height` (`AssertionError` otherwise); packs RGB565 (§9.2); if `stride > format_width` zero-pads each row to `stride`; returns little-endian bytes — size `stride * format_height * 2` |
| `convert_art_for_ipod(art_bytes, format_id) -> dict \| None` | `ALL_KNOWN_FORMATS[format_id]` first (unknown id → `KeyError`); `image_from_bytes` failure → `None`; `stride = IPOD_STRIDE_OVERRIDE.get(format_id, format_w)`; resize via `resize_for_format`; encode via `rgb888_to_rgb565`. Returns `{"data": bytes, "width": w, "height": h, "size": len(data), "format_width": w, "format_height": h}` |
| `_extract_format_ids(data)` | thin alias of `extract_format_ids` (§6.5) |

### 10.3 RGB565 reference constants

Encoding header comment: *5 bits red \| 6 bits green \| 5 bits blue, 16 bits
per pixel, little-endian* — the format used by Classic/Nano album art.

---

## 11. `podsync.artworkdb_writer.art_extractor`

### 11.1 Module surface

```text
MUTAGEN_AVAILABLE: bool          # True iff `import mutagen` succeeds
extract_art(file_path: str) -> bytes | None
find_folder_art(file_path: str) -> str | None
extract_art_with_source(file_path: str) -> tuple[bytes | None, str | None]
extract_art_with_folder(file_path: str) -> bytes | None
art_hash(art_bytes: bytes) -> str
```

Constants:

```text
_FOLDER_ART_NAMES = ("cover", "folder", "album", "front", "artwork", "thumb")
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp")
_VIDEO_EXTS = (".m4v", ".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv",
               ".mpg", ".mpeg", ".3gp", ".3g2", ".flv", ".mts", ".m2ts",
               ".ts", ".ogv")
```

`mutagen` import is wrapped in `try/except ImportError`; on failure set
`MUTAGEN_AVAILABLE = False` and log
`WARNING "mutagen not installed - art extraction disabled"` at import time.
**mutagen is not installed in this project**, so all tag paths below return
`None` here — implement them with lazy `from mutagen…` imports so the module
still imports cleanly and the folder/image paths work.

### 11.2 `extract_art` dispatch

Extension = `Path(file_path).suffix.lower()`. In order:

1. `_IMAGE_EXTS` → `Path(file_path).read_bytes()` (works without mutagen).
2. `_VIDEO_EXTS` → embedded cover only: for `.m4v`, `.mp4`, `.mov` and
   `MUTAGEN_AVAILABLE`, return `_extract_mp4(file_path)` (first `covr`
   element, `None` when absent); every other video extension, or no
   mutagen → `None`. There is **no** poster-frame fallback (no ffmpeg, §14),
   so a video without an embedded `covr` simply has no artwork.
3. `MUTAGEN_AVAILABLE` is `False` → `None`.
4. Tag extraction by extension:

| Ext | Helper | Source of bytes |
|---|---|---|
| `.mp3` | `_extract_mp3` | first ID3v2 frame whose key starts with `APIC` and `.data` non-empty |
| `.m4a .m4p .m4b .aac .alac` | `_extract_mp4` | first element of `covr` atom |
| `.flac` | `_extract_flac` | `pictures[0].data` |
| `.ogg` | `_extract_ogg` | `METADATA_BLOCK_PICTURE` via FLAC `Picture` base64 decode |
| `.opus` | `_extract_opus` | same as ogg |
| `.aif .aiff` | `_extract_aiff` | first `APIC*` frame data |
| anything else (`.wma`, `.wav`, …) | `_extract_generic` | `mutagen.File`, then first `APIC*` frame with `.data`, else `covr[0]` |

Failure semantics (the whole dispatch, steps 1–4, runs inside one `try`, so
`extract_art` never raises): `UnicodeError` → `DEBUG "ART: Could not parse embedded art
from {path}: {e}"`, return `None`; any other `Exception` → `WARNING "ART:
Failed to extract art from {path}: {e}"`, return `None`. Every helper returns
`None` (never raises) when tags are absent.

### 11.3 Folder art and sources

`find_folder_art(file_path)`: list the parent directory (`OSError` → `None`),
build a lowercase→actual-name map, then for **each name in `_FOLDER_ART_NAMES`
outer loop, each image extension inner loop**, return the first hit as
`os.path.join(directory, actual_name)`; none → `None`.

`extract_art_with_source(file_path) -> (bytes|None, str|None)`: embedded art
first (source = `file_path`), else folder art file bytes (source = folder
path), else `(None, None)`; folder read errors are swallowed.

`extract_art_with_folder(file_path) -> bytes|None`: `extract_art_with_source`
discarding the source.

`art_hash(art_bytes) -> str`: `hashlib.md5(art_bytes).hexdigest()` — tracks
with identical art share one ArtworkDB entry.

---

## 12. `podsync.artworkdb_writer.artwork_writer`

The orchestrator. Logger: `podsync.artworkdb_writer.artwork_writer`.

### 12.1 Public types defined here

| Name | Shape |
|---|---|
| `ArtworkDecisionKind(StrEnum)` | `NEW_FROM_PC="new_from_pc"`, `PRESERVE_EXISTING="preserve_existing"`, `CLEAR_ART="clear_art"`, `PRESERVE_FALLBACK="preserve_fallback"` |
| `ArtworkAssetRef` (frozen) | `(source: str, value: str\|int)` — identity of a shared payload: `("pc", md5)`, `("preserve", joined-locations-or-img_id)` |
| `TrackArtworkDecision` (mutable) | `db_track_id, kind, asset_ref=None, art_bytes=None, src_img_size=0, source_path="", existing_entry=None` |
| `ArtworkDecisionSummary` (mutable) | counters `preserved_unchanged, preserved_fallback, reencoded, cleared, shared_from_album, salvaged, dropped_invalid` (all int, default 0) |
| `ExistingArtworkFormats` (mutable) | `required_known: dict[int, ExistingFormatRef]`, `extra_known: dict[int, ExistingFormatRef]`, `unknown_passthrough: dict[int, PassthroughFormatRef]`, `known_present: set[int]` |
| `PendingArtworkWrite` | §12.10 |

Module constant: `ITHMB_MAX_SIZE_BYTES = 32 * 1000 * 1000`.

Module hook: `extract_art_with_folder = <the art_extractor function>`. Tests
may **rebind** `podsync.artworkdb_writer.artwork_writer.extract_art_with_folder`;
the decision loop must detect rebinding by identity
(`extract_art_with_folder is not _extract_art_with_folder`) and, if rebound,
call the rebound callable returning `bytes|None` with source = `pc_path`;
otherwise call `extract_art_with_source(pc_path)`.

### 12.2 Track field access (`_get_track_field`)

Tracks may be dicts or objects:

* `db_track_id`: dict → `track.get("db_track_id", track.get("db_id"))`;
  object → `getattr(track, "db_track_id", getattr(track, "db_id", None))`.
* Any other field: `track.get(field)` / `getattr(track, field, None)`.
* Sync hint: field `_iop_artwork_sync_hint`, `str(... or "").strip().lower()`.

### 12.3 Per-track decisions (`_collect_track_artwork_decisions`)

Inputs: `tracks`, normalized `pc_file_paths` (`db_track_id -> path`, only keys
that coerce to `int > 0`), `existing_art` (§7.4 shape). Returns
`(decisions: dict[int, TrackArtworkDecision], summary)`.

First build `existing_by_song_id`: for every existing entry (in ArtworkDB
order) with non-zero `song_id`, map `song_id -> img_id`, **first entry
wins** (same rule as chapter 02 §11). Existing-entry resolution for a
track, with `ref` = the first non-zero of the track fields `mhii_link`,
`mhiiLink`, `artwork_id_ref`:

1. `ref` present in `existing_art` **and** that entry's `song_id` equals
   the track's `db_track_id` → that entry (the track's own link is kept
   even when the song owns several MHIIs — observed on iTunes-written
   databases; preferring the song map here would re-point valid links);
2. otherwise `existing_by_song_id[db_track_id]`;
3. otherwise `ref`, accepted only if present in `existing_art`.

Decision precedence (first match wins), in evaluation order:

| # | Condition | Decision |
|---|---|---|
| 1 | no `db_track_id` (falsy) | track skipped entirely; `WARNING "ART: track '{title}' has no db_track_id, skipping"` (title fallback `"?"`) |
| 2 | hint `== "clear_art"` | `CLEAR_ART` (summary `cleared += 1`) |
| 3 | hint `== "preserve_existing"` and existing entry | `PRESERVE_EXISTING` if `pc_path` given **and** `os.path.exists(pc_path)`, else `PRESERVE_FALLBACK`; `asset_ref = _preserved_asset_ref(...)`; `src_img_size = existing_entry["src_img_size"] or 0` |
| 4 | no `pc_path` in map | existing → `PRESERVE_FALLBACK` (asset/src as above); else `CLEAR_ART` |
| 5 | `pc_path` missing on disk | `WARNING "ART: PC file not found for '{title}': {path}"`; existing → `PRESERVE_FALLBACK`; else `CLEAR_ART` |
| 6 | extraction returns `None` | `CLEAR_ART` |
| 7 | otherwise | `NEW_FROM_PC`, `asset_ref = ArtworkAssetRef("pc", art_hash(bytes))`, `art_bytes`, `src_img_size = len(art_bytes)`, `source_path` from extraction (fallback `pc_path`) |

Summary counters: `PRESERVE_EXISTING` → `preserved_unchanged += 1`;
`PRESERVE_FALLBACK` → `preserved_fallback += 1`; `CLEAR_ART` (rows 2, 4–6)
→ `cleared += 1`; `NEW_FROM_PC` → `reencoded += 1`. Every decision keeps the
resolved `existing_entry` (also for `CLEAR_ART` and `NEW_FROM_PC`), so the
classification logs of §12.5 still see it.

Extraction cache: keyed by `os.path.normcase(os.path.abspath(pc_path))` within
one call; value `(bytes|None, source_path|None)`.

`_preserved_asset_ref(existing_img_id, existing_entry)`:
for each format id in `entry["formats"]` sorted by `int(id)`, build
`f"{fmt}:{filename}:{offset}:{size}"` where `filename =
ref.ithmb_filename or ithmb_filename_from_path(ref.path, fmt)`; join with `"|"` →
`ArtworkAssetRef("preserve", joined)`; if there are no formats →
`ArtworkAssetRef("preserve", existing_img_id)`.

### 12.4 Format classification (`_classify_existing_entry_formats`)

```text
_classify_existing_entry_formats(existing_entry, required_format_ids,
                                 device_format_defs) -> ExistingArtworkFormats
```

* `existing_entry is None` → empty result.
* Known := `_resolve_known_format_definition(fmt_id, device_format_defs)` =
  `device_format_defs.get(fmt_id) or ITHMB_FORMAT_MAP.get(fmt_id)` (device
  override **first**, global second).
* Non-integer format keys → `WARNING "ART: ignoring non-integer artwork format id {key!r}"`, skip.
* Unknown format → `_normalize_passthrough_format_ref`: keep only if
  `ref.path` non-empty, `ref.size > 0`, and `os.path.exists(ref.path)`;
  else `WARNING "ART: cannot preserve passthrough format {id} as-is; missing file or size ({path})"` and drop. Kept → `unknown_passthrough[id] = PassthroughFormatRef`.
* Known format → add to `known_present`, then `_validate_existing_format_ref`:
  * Missing path / `size ≤ 0` / file absent → `WARNING "ART: existing known
    format {id} cannot be preserved; missing file or size ({path})"`, result
    `None` → `WARNING "ART: existing format {id} has invalid geometry or
    payload size; will attempt regeneration if needed"`, **not** bucketed
    (but remains in `known_present`).
  * Otherwise compute the two candidate expected sizes
    `expected_size_bytes(fmt, ref.stride_pixels, ref.stored_height, stride_pixels=ref.stride_pixels, fmt_override=...)`
    and
    `expected_size_bytes(fmt, ref.width, ref.stored_height, stride_pixels=default_stride_pixels(fmt, ref.width, fmt_override=...), fmt_override=...)`,
    discard zeros; if `ref.size` is outside that set → `DEBUG "ART: existing
    known format {id} has size {size} outside expected sizes {sorted-set};
    carrying forward on-device bytes"` — **the ref is still returned** (kept).
  * Bucket: `fmt_id in set(required_format_ids)` → `required_known`, else → `extra_known`.

`_log_unknown_existing_formats` / `_log_extra_known_existing_formats`: emit at
most once per `(fmt_id, path)`:

* `WARNING "ART: encountered unknown artwork format {id} at {path}; leaving its ithmb file untouched"`
* `WARNING "ART: encountered extra known artwork format {id} at {path}; preserving/regenerating it because it is present on-device"`

### 12.5 Rewrite targets (`_collect_rewrite_targets`)

```text
_collect_rewrite_targets(decisions, required_format_ids, device_format_defs)
    -> (targets: dict[ArtworkAssetRef, list[int]],
        passthrough: dict[ArtworkAssetRef, dict[int, PassthroughFormatRef]])
```

For every decision (classification + warn-once logs always run, even for
`CLEAR_ART`):

* If `asset_ref is None` → contribute nothing.
* `targets[asset]` (as a sorted list) = `required_format_ids ∪ known_present`
  of the decision's existing entry.
* `passthrough[asset]` = the entry's `unknown_passthrough` map, **only** when
  `decision.kind ∈ {PRESERVE_EXISTING, PRESERVE_FALLBACK}`.

(Chapter 05 expectation: unknown IDs are never decoded or rewritten; known IDs
— required *and* extra-on-device — are writer-owned.)

### 12.6 Conversion of new PC art (`_convert_new_pc_art`)

Collects unique `asset -> art_bytes` from `NEW_FROM_PC` decisions, emits
`progress(f"Artwork — converting {n} image{s}")` (plural `s` iff `n != 1`),
then converts each asset concurrently with `ThreadPoolExecutor` using
`max_workers = max(1, min(len(map), os.cpu_count() or 4))`. Per asset:

1. `image_from_bytes(art_bytes, source_path=...)`; if this raises
   `TypeError` whose text contains `"source_path"`, retry as
   `image_from_bytes(art_bytes)` (monkeypatch seam); `None` → asset dropped.
2. For each target format id (from §12.5, else `required_format_ids`):
   dims via `_target_dimensions_for_format` (`device_formats[fmt_id]` if
   present, else known-definition dims, else `None` →
   `WARNING "ART: format {id} is not encodable for {asset}; skipping"`); encode
   via `encode_image_for_format(img, fmt_id, *dims, fmt_override=device_format_defs.get(fmt_id))`.
   Exceptions: `WARNING` if the format is required, `DEBUG` otherwise, message
   `"ART: format {id} conversion failed for {asset}: {exc}"`.
3. If **any required** format is missing after the loop → drop the asset with
   `WARNING "ART: dropping rewritten artwork {asset} because required formats {missing} could not be generated"`.
4. Success → `ArtworkPayload(formats=..., src_img_size=len(art_bytes))`.

### 12.7 Preserved/salvaged payloads (`_load_preserved_art_payloads`)

```text
_load_preserved_art_payloads(decisions, required_format_ids,
    asset_target_format_ids, asset_passthrough_format_refs,
    device_formats, device_format_defs, *,
    passthrough_known_formats: bool = False,
    rewrite_known_filenames: Mapping[int, set[str]] | None = None)
    -> (dict[ArtworkAssetRef, ArtworkPayload], salvaged: int, dropped: int)
```

1. Group preserve decisions by asset (first wins). `fmt_meta` =
   `required_known ∪ extra_known` of the classification. Register each fmt ref
   for bulk reading when `_should_rewrite_known_ref` says so: with
   `passthrough_known_formats=False` → always; `True` (the writer's setting)
   → only when the ref's filename ∈ `rewrite_known_filenames.get(fmt, ∅)`.
   Always record `passthrough[id]` maps from §12.5.
2. Bulk pixel reads: per `(ithmb_path, fmt)` sort refs by offset, open the
   file once, `seek`+`read(size)`; a short read logs `DEBUG "ART: short read
   for preserved {asset} fmt {id}"`; `OSError` → `WARNING "ART: failed to read
   preserved ithmb {path}: {exc}"`.
3. Build payloads: passthrough refs always carried; rewrite-target formats get
   `EncodedFormatPayload.from_existing_ref(ref, pixel_bytes)` when bytes were
   read, otherwise (not a rewrite target) `PassthroughFormatRef`. Asset kept
   if it has ≥ 1 format → `ArtworkPayload(formats, src_img_size)`.
4. **Salvage pass**: for each preserve asset still missing any target format:
   try each of its existing refs whose format id is *known* (device defs or
   global): read `ref.size` bytes at `ref.ithmb_offset`; decode with
   `_decode_preserved_frame(ref, fmt_id, bytes, fmt_override=...)` (which
   simply forwards to `decode_pixels_for_format(format_id, pixel_bytes,
   ref.width, ref.height, ref.hpad, ref.vpad, fmt_override=...)`); first
   non-`None` image wins (`OSError`/short read → try next ref).
   * No source image → remove asset from results, `dropped += 1`.
   * Else encode each missing target (same rules/logging as §12.6; no
     dimensions → `WARNING "ART: format {id} is not encodable for preserved artwork {asset}; skipping"`;
     required failure logs at WARNING, others DEBUG, message
     `"ART: salvage re-encode failed for {asset} fmt {id}: {exc}"`).
   * All required formats present → keep; `salvaged += 1` if anything new was
     encoded. Otherwise remove, `dropped += 1`.

Note: `_decode_preserved_frame` and `_target_dimensions_for_format` must be
**module-level** functions called through module globals (tests rebind them).

### 12.8 ITHMB rewrite plan (`_select_ithmb_rewrite_plan`)

```text
_select_ithmb_rewrite_plan(existing_art, decisions, new_artwork)
    -> (rewrite_filenames: dict[int, set[str]],
        writable_start_indices: dict[int, int])
```

Only called when there is at least one newly encoded payload (before preserved
payloads are merged). Rules:

* `_ithmb_file_index(filename, fmt)` parses `N` from the canonical
  `F{fmt}_{N}.ithmb` (`None` if the name doesn't match exactly). Index maps are
  built from every existing ref (its `ithmb_filename`, else derived from
  `path`).
* Preserved bytes per `(fmt, filename)`: sum of `ref.size` over
  `PRESERVE_*` decisions, **deduplicated per asset**, only canonically named
  files.
* New bytes per format: sum of `EncodedFormatPayload.size` in `new_artwork`.
* For each format with new payloads, scan `index` in
  `1 .. max_existing_index + 1`:
  * first pass: `preserved(fmt, file) + total_new ≤ ITHMB_MAX_SIZE_BYTES` →
    `rewrite_filenames[fmt] = {filename}`, `writable_start_indices[fmt] = index - 1`, stop.
  * second pass (if first failed): same scan but only
    `preserved + largest_new_payload ≤ MAX` (writer opens further shards lazily).
  * neither → `RuntimeError(f"Artwork format {format_id} has a {largest}-byte payload, exceeding the {MAX}-byte ITHMB file limit.")`.

The chosen filename is the **lowest-numbered** shard that fits; preserved
refs in other shards stay untouched (passthrough).

### 12.9 `write_artworkdb` — main entry

```text
write_artworkdb(ipod_path: str, tracks: list,
                pc_file_paths: dict | None = None,
                start_img_id: int = 100,
                reference_artdb_path: str | None = None,
                artwork_formats: dict[int, tuple[int, int]] | None = None,
                defer_commit: bool = False,
                progress_callback: Callable[[str], None] | None = None,
                before_device_mutation: Callable[[], None] | None = None,
                ) -> dict | PendingArtworkWrite
```

Pipeline (in order):

1. `artwork_subtree = os.path.join("iPod_Control", "Artwork")`;
   `artwork_dir = str(resolve_device_path(ipod_path, artwork_subtree, allowed_subtree=artwork_subtree))`.
   If the dir doesn't exist: call `before_device_mutation()` (if given), then
   `os.makedirs(..., exist_ok=True)`.
2. Normalize `pc_file_paths` → `{int(key): str(path)}` keeping only keys
   coercible to `int > 0` (bad keys silently skipped).
3. Formats: `artwork_formats` if given, else `get_artwork_formats(ipod_path)`;
   `device_format_defs = get_artwork_format_definitions(ipod_path)`;
   `required_format_ids = sorted(device_formats.keys())`. If empty →
   `RuntimeError("No artwork format definitions are available for this iPod; cannot write ArtworkDB safely.")`.
   `INFO "ART: using formats {ids}"` is logged just before that check (so it
   also appears, with `[]`, when the write is refused).
4. If `reference_artdb_path` exists → read whole file as `ref_mhfd` bytes
   (header field donor for `build_artworkdb`); else `None`.
5. `existing_art = read_existing_artwork(<artwork_dir>/ArtworkDB, artwork_dir)`
   (§7.4); if non-empty log `INFO "ART: read {n} existing image entries from ArtworkDB"`.
6. Progress `f"Artwork — scanning {len(tracks)} tracks"`, then decisions
   (§12.3), then progress via `_format_artwork_decision_progress` (table
   below), then `INFO "ART decisions: preserve={a} fallback={b} reencode={c} clear={d}"`.
7. Rewrite targets + passthroughs (§12.5).
8. Convert new PC art (§12.6).
9. If there are new payloads → compute the rewrite plan (§12.8); else both
   results are `{}`.
10. Load preserved payloads (§12.7) with `passthrough_known_formats=True` and
    `rewrite_known_filenames=rewrite_filenames`; merge into the payload map
    (`update`); store `summary.salvaged` and accumulate `summary.dropped_invalid`.
    When `salvaged > 0` log
    `INFO "ART: salvaged {n} preserved artwork entry"` (`entries` when `n != 1`)
    `" via decode/re-encode fallback"`.
11. **Assign entries**: iterate `tracks` in order; skip tracks without
    `db_track_id`, without a decision, with `CLEAR_ART`, without
    `asset_ref`, or whose asset has no payload (the last case increments
    `dropped_invalid`). Each surviving track gets the next `img_id` starting at
    `start_img_id`, stride 1, as `ArtworkEntry(img_id, db_track_id,
    art_hash_or_None, src_img_size, formats, [db_track_id])` where `art_hash` is
    the digest only for `("pc", ...)` assets (`None` for preserved).
    Log `INFO "ART result: {len(entries)} live entries from {n_unique} unique payloads ({dropped} dropped invalid)"`.
    **Old mhii ids are never reused** — the caller rewrites every track's
    `mhii_link` from the returned map.
12. Progress messages (see table) — emitted before writing.
13. Compute per-format `image_sizes[fmt]` = **mode** (most common) of all
    non-zero payload sizes for that format across entries
    (`Counter(...).most_common(1)`); if more than one distinct size →
    `WARNING "ART: format {id} has mixed payload sizes {sorted}; using most common {n} in MHIF"`.
14. **Write ithmb shards** (§12.11) — only formats that have at least one
    `EncodedFormatPayload` in live entries are writable; formats that are
    passthrough-only are logged
    `INFO "ART: preserving passthrough-only formats without rewriting files: {ids}"`.
15. `next_id = start_img_id + len(entries)`; `artdb_data = build_artworkdb(
    entries, format_locations_map, format_ids (sorted union across entries),
    image_sizes, next_id, ref_mhfd)`; write it to a fresh sibling temp of
    `<artwork_dir>/ArtworkDB` (`open_unique_sibling_temp(..., "wb")`, then
    `flush_written_file`). Any failure here unlinks **all** pending ithmb
    temps and the ArtworkDB temp (each unlink preceded by
    `before_device_mutation()` if provided) and re-raises.
16. Build `db_track_id_to_art_info = {entry.db_track_id: (entry.img_id,
    entry.src_img_size)}` (live entries only).
17. `pending_renames` = ithmb `(temp, final)` pairs sorted by `(fmt_id,
    file_index)`, then the ArtworkDB pair last.
18. If `defer_commit=True` → log
    `INFO "ART: prepared {n_unique} unique images, {n_entries} MHII entries (per-track) — commit deferred"`
    and return `PendingArtworkWrite` (§12.10) **without renaming anything**. Otherwise, for each pair: `before_device_mutation()`
    (if given) then `durable_replace(temp, final)`; on exception unlink all
    remaining temps (with revalidation hook) and re-raise.
19. Log `INFO "Wrote ithmb files: {n_unique} unique images, {n_entries} MHII entries (per-track)"`,
    then one `INFO` per written file `"  {basename}: {size} bytes"` and
    `"  F{id}_N.ithmb: preserved in place"` per passthrough-only format.
20. Return `db_track_id_to_art_info`.

On any exception during shard writing: all shard temps are unlinked (with the
mutation hook) and the exception propagates.

#### Progress message table

| Emission point | Exact message |
|---|---|
| start | `Artwork — scanning {len(tracks)} tracks` |
| decisions, nothing to report | `Artwork — verifying existing artwork links` |
| decisions, otherwise | `Artwork — verifying artwork links ({parts joined by ", "})` where parts are (in this order, only non-empty): `preserving {preserved_unchanged + preserved_fallback} existing`, `updating {reencoded} changed/new`, `clearing {cleared}` |
| conversion | `Artwork — converting {n} image` + (`s` if `n != 1`) |
| writing, both writable and preserved | `Artwork — writing {w} changed/new image[s], preserving {p} existing` |
| writing, only writable | `Artwork — writing {w} changed/new image[s]` |
| index update, payloads exist but nothing re-encoded | `Artwork — updating artwork index ({n} existing image[s], no image data rewritten)` |
| index update, only clears | `Artwork — updating artwork index (clearing artwork links)` |
| index update, otherwise | `Artwork — updating artwork index (no live artwork)` |

(`w` = count of unique assets with ≥ 1 `EncodedFormatPayload`;
`p` = other unique live assets; `n` = all unique live assets.)

### 12.10 `PendingArtworkWrite`

Mutable dataclass: `db_track_id_to_art_info: dict`, `_pending_renames: list` of
`(temp, final)`, `_post_commit_cleanup: Callable | None = None`,
`_committed: bool = False`.

* Property `db_id_to_art_info` → alias of `db_track_id_to_art_info`.
* Dict-like protocol over the mapping: `__getitem__`, `__setitem__`,
  `__contains__`, `__iter__`, `__len__`, `get`, `keys`, `values`, `items`.
* `commit(before_replace: Callable[[], None] | None = None) -> None`:
  idempotent (returns immediately if `_committed`); for each `(temp, final)`
  in order: call `before_replace()` (if given) then `durable_replace(temp,
  final)`; then run `_post_commit_cleanup()`; set `_committed = True`.
  The interleave contract is *revalidate → replace, per pair*.
* `abort(before_remove: Callable[[], None] | None = None) -> None`:
  no-op after a commit; otherwise, for each temp: call `before_remove()` (if
  given) then `durable_unlink(temp, missing_ok=True)`, swallowing `OSError`
  per temp. It sets no flag, so a repeated abort simply retries the (already
  missing) unlinks. `_post_commit_cleanup` is **not** run.

`durable_replace`/`durable_unlink` are resolved as **module globals of
`artwork_writer`** (tests rebind `artwork_writer.durable_replace`).

### 12.11 Shard writing (inner mechanics)

State per writable format: `{"index": int, "offset": int}` with
`index = writable_start_indices.get(fmt, 0)`, `offset = 0`.

* `_open_next_ithmb(fmt)`: close/flush the current handle
  (`flush_written_file` then `close`), then loop `index += 1` until
  `F{fmt}_{index}.ithmb` is **not** in
  `protected_passthrough_filenames[fmt]` (set of filenames referenced by
  `PassthroughFormatRef`s of live entries — those files must never be
  rewritten); `offset = 0`; final path = `artwork_dir / filename`; call
  `before_device_mutation()`; open via `open_unique_sibling_temp(final,
  mode="wb")`; record temp+final paths keyed `(fmt, index)`.
* `_write_encoded_ithmb_payload(fmt, data)`: open the first shard if none;
  else if `offset > 0 and offset + len(data) > ITHMB_MAX_SIZE_BYTES` → open
  the next shard; append `data` at `offset`, advance, return
  `IthmbLocation(filename, offset)`. (A first payload larger than the budget
  is still written to shard N=1 — the budget check happens only when
  `offset > 0`.)
* Per unique asset, in entry order: if the asset was written already, reuse
  its location map; otherwise, for `fmt in sorted(format_ids)`: encoded →
  append and record; passthrough → `IthmbLocation(existing filename,
  existing offset)` without touching the file.
* All handles are flushed/closed in a `finally`.

Frames are concatenated with **no alignment padding between frames** —
`ithmbOffset` values are exact byte sums of previous payload lengths.

### 12.12 Monkeypatch seams in `artwork_writer` (tests depend on these)

These must be module-level names, and *calls must go through the module
globals* (no local rebinding, no `from ... import x` inside functions):

`read_existing_artwork`, `get_artwork_format_definitions`,
`get_artwork_formats` (only used when `artwork_formats is None`),
`expected_size_bytes`, `extract_art_with_folder` (plus the identity check
against `_extract_art_with_folder`), `image_from_bytes`,
`encode_image_for_format`, `_decode_preserved_frame`, `_target_dimensions_for_format`,
`ITHMB_MAX_SIZE_BYTES`, `durable_replace`, `durable_unlink`,
`flush_written_file`, `open_unique_sibling_temp`, `resolve_device_path`,
plus the inherited module names `os`, `ArtworkFormat`, `ExistingFormatRef`,
`EncodedFormatPayload`, `PassthroughFormatRef`, `ArtworkEntry`,
`IthmbLocation`, `ArtworkPayload`, `build_artworkdb`, `decode_pixels_for_format`,
`default_stride_pixels`, `format_dimensions`, `art_hash`,
`extract_art_with_source`.

---

## 13. Exceptions and side effects (summary)

### 13.1 Exceptions raised by this chapter's modules

| Exception | Where | Condition |
|---|---|---|
| `TypeError` | `parse_artworkdb` | argument neither `str` nor file-like |
| `ValueError("Unknown chunk type: …")` | `parse_chunk` | unregistered tag |
| `UnicodeDecodeError` | `parse_chunk` | non-UTF-8 tag bytes (strict) |
| `ValueError("ArtworkDB chunk header outside buffer at offset …")` | `read_chunk_header` | header outside buffer |
| `DeviceWriteSafetyError` (subclass of `RuntimeError`, from `podsync.device.write_guard`) | `read_existing_artwork` | any `OSError` other than `FileNotFoundError`, or any structural violation (§7.4; contract substring `ArtworkDB is malformed or truncated`) |
| `ValueError("MHNI padding too large for format …")` | `_write_mhni` | vpad or hpad > 32767 |
| `ValueError("Unsupported unknown pixel format for format_id=…")` | `encode_image_for_format` | `pixel_format == "UNKNOWN"` |
| `RuntimeError("No artwork format definitions are available …")` | `write_artworkdb` | no required formats |
| `RuntimeError("Artwork format … exceeding the …-byte ITHMB file limit.")` | `_select_ithmb_rewrite_plan` | no shard can hold a payload |
| `ValueError("Artwork image exceeds Pillow safety limit: …" [+ " Offending image: …"])` | `image_from_bytes` | Pillow `DecompressionBombError` |
| `AssertionError` | `rgb888_to_rgb565` | image size ≠ expected box |
| `KeyError` | `convert_art_for_ipod` | unknown `format_id` (direct dict index) |
| `ValueError("Unknown format ID: …")` | `resize_for_format` | id not in `ALL_KNOWN_FORMATS` |
| `UnsafeDevicePathError` (ValueError) | `resolve_device_path` (device pkg) | artwork path escapes `iPod_Control/Artwork` |

### 13.2 Files written (side effects)

All writes target `<ipod_path>/iPod_Control/Artwork/`:

| Final file | Content | When |
|---|---|---|
| `ArtworkDB` | complete chunk tree per §2 (always written, even with zero entries) | every successful `write_artworkdb` |
| `F<fmt>_<N>.ithmb` | headerless concatenated frames per §3.1 | only formats with encoded payloads; lowest fitting `N` chosen first (§12.8); files not in the rewrite plan — including all passthrough/unknown-format files — are **never truncated, pruned, or replaced** |
| `.iop-*.tmp` (siblings) | transient; renamed via `durable_replace` or unlinked on failure/abort | during write; must not exist after success/failure cleanup; predictable names like `ArtworkDB.tmp` must never be opened/truncated |

No checksums, hashes, or footer/manifest records are written into ArtworkDB or
ithmb files by this subsystem (the `art_hash` MD5 exists only in memory as a
dedupe key). Durability (fsync/dir fsync/Windows `FlushFileBuffers`) is
performed by the `podsync.device.durability` helpers listed in §4.3.

Atomicity: all final files are fully written and flushed to unique sibling
temps first; the renames happen last, ithmb shards in `(fmt, index)` order,
`ArtworkDB` last, so a crash mid-commit leaves the old ArtworkDB pointing at
old (still intact) shards.

---

## 14. Scope exclusions

These must **not** be implemented in this chapter's modules:

1. **ffmpeg / transcoder path in `art_extractor`.** No `_find_ffmpeg`,
   no `_extract_video_frame`, no import of `podsync.sync.transcoder`, no
   `subprocess`/`tempfile` frame-grabbing. Video inputs get only their
   embedded MP4 `covr` cover (§11.2); anything that would need a decoded
   frame returns `None`.
2. **Poster frames for video**: no frame extraction, seeking or scaling of
   video streams of any kind. Only the embedded `covr` atom of MP4-family
   video (`.m4v/.mp4/.mov`) is used, through the same `_extract_mp4` helper as
   audio.
3. **Imports of `gui` / `application` / `podcasts` / `sync.transcoder` /
   `sqlitedb_writer`** (or any sibling package outside
   `artworkdb_*` + the documented `podsync.device` surface): none of the
   modules in this chapter may import them.
4. **Photo-database features**: `mhba`/`mhia` album parsing beyond returning
   `{}`, and `podsync.device.artwork.photo_formats_for_device` (chapter 06's
   concern) — ArtworkDB here carries music artwork only (`mhla` is written empty).
5. **mutagen-dependent tag parsing is present in spec but inert** in this project
   (dependency not installed): those paths return `None` via the
   `MUTAGEN_AVAILABLE` guard; do not vendor or reimplement ID3/MP4 parsing by
   hand.

---

## 15. Quick cross-chapter dependencies

| Needed from | Symbol | Specified in |
|---|---|---|
| `podsync.device` | `ArtworkFormat`, `ITHMB_FORMAT_MAP`, `ITHMB_SIZE_MAP`, `ithmb_formats_for_device`, `resolve_cover_art_format_definitions_for_device`, `get_current_device_for_path` | chapter 06 (values copied in §4.2) |
| `podsync.device.artwork_presets` | `artwork_format_candidates`, override tuples | chapter 06 (copied in §4.2) |
| `podsync.device.durability` | `open_unique_sibling_temp`, `flush_written_file`, `durable_replace`, `durable_unlink` | chapter 06/07 (semantics §4.3) |
| `podsync.device.path_safety` | `resolve_device_path`, `UnsafeDevicePathError` | chapter 06/07 |
| `podsync.device.write_guard` | `DeviceWriteSafetyError` | chapter 06/07 |
| this chapter | `write_artworkdb` return map → iTunesDB `mhii_link`/`artwork_size` per track | consumed by the sync/iTunesDB writer chapters |
