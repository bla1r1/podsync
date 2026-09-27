# 01 — Overview: what podsync is and how it is put together

`podsync` is a pure-Python engine that reads and writes the databases an
iPod keeps in `iPod_Control\iTunes` (the iTunesDB family) and
`iPod_Control\Artwork` (the ArtworkDB family), plus the device-identity
and write-safety machinery around them. No iTunes, no third-party iPod
tools — everything is done by producing the same bytes the device itself
stores.

## 1. Big picture

An iPod mounted as a drive is just a filesystem with a handful of
database files:

```
<iPod>/iPod_Control/iTunes/iTunesDB        track & playlist database  (mhbd…)
<iPod>/iPod_Control/iTunes/iTunesSD        iPod Shuffle database (not our scope)
<iPod>/iPod_Control/iTunes/iTunesPrefs…    preferences (not our scope)
<iPod>/iPod_Control/iTunes/Play Counts     per-track play counts (separate file)
<iPod>/iPod_Control/Artwork/ArtworkDB      artwork index (mhfd…)
<iPod>/iPod_Control/Artwork/*.ithmb        packed image blobs
<iPod>/iPod_Control/Device/SysInfoExtended device identity text
```

Both databases are trees of **chunks**: every chunk starts with a
12-byte header — a 4-byte ASCII tag (`mhbd`, `mhsd`, `mhlt`, `mhit`,
`mhod`, `mhyp`, `mhlp`, `mhia`, `mhii`, `mhli`, `mhip`, `mhfd`,
`mhni`, `mhlf`, …) followed by little-endian `total length` and
`number of children` uint32 fields (`<4sII>`), then type-specific
payloads. Strings are UTF-8 or UTF-16LE with explicit byte-length and
(usually) terminator rules. Timestamps use the Mac epoch (seconds since
1904-01-01 UTC), stored little-endian.

The specification of these layouts is split across chapters:

* **byte layouts and constants** — chapter 03 (`itunesdb_shared`),
  chapters 02/04/05 for the exact fields each parser/writer touches;
* **reading** — chapter 02 (`itunesdb_parser`);
* **writing** — chapter 04 (`itunesdb_writer`, incl. database
  checksums/hashes);
* **artwork** — chapter 05 (`artworkdb_*`);
* **the device as an object** — chapters 06 (identity/discovery) and
  07 (write safety/filesystem);
* **orchestration** — chapter 08 (`sync`): read → decide → write →
  read-back-verify.

## 2. Package layout (exact — tests import these paths)

The implementer must create every file below. Module paths in the tests
are `podsync.<path with / replaced by .>` minus `.py`.

```
podsync/
  __init__.py
  itunesdb_parser/
    __init__.py            parser package entry (parse_itunesdb re-export)
    _parsing.py            low-level readers shared by the mhXX parsers
    chunk_parser.py        generic chunk-tree walker
    parser.py              top-level iTunesDB parse orchestration
    mhbd_parser.py         database header
    mhsd_parser.py         dataset header
    mhit_parser.py         track item
    mhod_parser.py         string/rule/property container (all mhod kinds)
    mhyp_parser.py         playlist header
    mhia_parser.py         album item
    mhii_parser.py         artist item (iTunesDB meaning of the mhii tag)
    mhip_parser.py         playlist item (track-in-playlist reference)
    playcounts.py          Play Counts file parse + merge
    otg.py                 On-The-Go playlist discovery
    artwork_links.py       hydrate track → artwork references
    forensics.py           structural diagnostics / reconstruction
    byte_walk.py           byte-level chunk indexing (JSON/document forms)
    ipod_library.py        convenience “load whole library” entry
    exceptions.py          parse-error hierarchy
  itunesdb_shared/
    __init__.py
    constants.py           mhod type codes, media types, flags
    field_base.py          primitive field read/write + generic headers
    extraction.py          string/field extraction helpers
    device_time.py         Mac-epoch conversions, testable time context
    mhbd_defs.py           layout of mhbd (authoritative)
    mhsd_defs.py           layout of mhsd
    mhit_defs.py           layout of mhit
    mhod_defs.py           layout of mhod + all mhod kind codes (authoritative)
    mhip_defs.py           layout of mhip
    mhii_defs.py           layout of mhii
    mhia_defs.py           layout of mhia
    mhyp_defs.py           layout of mhyp
    playlist_kinds.py      playlist kind classification
    playlist_hierarchy.py  folder/parent/dvd/menu reconciliation
    playlist_properties.py playlist property plist (On-The-Go etc.)
    playlist_lifecycle.py  playlist lifecycle rules
    album_identity.py      album identity matching
  itunesdb_writer/
    __init__.py            write_itunesdb orchestration + checksum dispatch
    mhit_writer.py         track writer + TrackInfo record
    mhod_writer.py         string container writer (incl. limits)
    mhod52_writer.py       mhod kind 0x52 writer
    mhod_spl_writer.py     smart-playlist rule serialization
    mhbd_writer.py         database header writer (datasets, artwork refs)
    mhsd_writer.py         dataset writer
    mhlt_writer.py         track list writer
    mhla_writer.py         album writer
    mhli_writer.py         image list writer
    mhip_writer.py         playlist item writer
    mhlp_writer.py         playlist list writer
    mhyp_writer.py         playlist writer + PlaylistInfo/PlaylistItemMeta
    hash58.py              database hash “58”
    hash72.py              database hash “72” (AES via pycryptodome)
    hashab.py              database hash “AB” — unsupported: raises a clear
                           error (chapter 04 §6.4); no wasm/ folder exists
  artworkdb_parser/
    __init__.py
    chunk_parser.py        ArtworkDB chunk walker
    constants.py           ArtworkDB-local constants
    parser.py              parse_artworkdb entry
    mhfd_parser.py         artwork database header
    mhsd_parser.py         artwork dataset header
    mhli_parser.py         image list
    mhii_parser.py         image item
    mhod_parser.py         artwork string container
    mhni_parser.py         image index (row/column/format hints)
  artworkdb_shared/
    __init__.py
    constants.py           artwork format ids, sizes
    binary.py              binary read/write helpers
    mhod.py                mhod container decode + type names
    mhni.py                mhni field reading, format inference
    mhlf.py                mhlf (format list) handling
    ithmb_paths.py         .ithmb filename resolution per device
  artworkdb_writer/
    __init__.py            write_artworkdb entry
    artwork_writer.py      full artwork write pipeline
    artworkdb_chunks.py    chunk builders
    artwork_types.py       image/record dataclasses
    ithmb_codecs.py        image codecs (RGB565 … ) for .ithmb packing
    rgb565.py              RGB565 encode + format-table resolution
    art_extractor.py       cover extraction from audio files/folders and
                           the embedded covr of m4v/mp4/mov (no ffmpeg frames)
  device/
    __init__.py            re-export surface of the names below
    info.py                DeviceInfo, current-device state, resolve_itdb_path
    scanner.py             platform scan for mounted iPods (dedup rules)
    models.py              IPOD_MODELS and identity mappings
    lookup.py              serial/model lookups, friendly names
    sysinfo.py             SysInfo/SysInfoExtended parsing → identity
    virtual.py             create_virtual_ipod etc.
    virtual_identity.py    identity of virtual devices
    capabilities.py        DeviceCapabilities / ArtworkFormat per family+gen
    checksum.py            ChecksumType tables (mhbd scheme ↔ hash)
    artwork.py             ITHMB format/size registries, per-device formats
    artwork_presets.py     ArtworkFormat registry data (chapter 05 §4)
    bootstrap.py           ensure an iTunes database skeleton exists
    usb_backend.py         USB backend selection (guarded imports)
    vpd_libusb.py          VPD queries over libusb (guarded imports)
    vpd_usb_control.py     USB control-message SysInfoExtended queries
    vpd_windows.py         Windows VPD path
    vpd_linux.py           Linux VPD path
    vpd_iokit.py           macOS VPD path (raises ImportError elsewhere)
    linux_identity.py      Linux identity mapping
    diagnostic_log.py      device diagnostic logging helpers
    write_guard.py         DeviceWriteGuard — armed writes only
    write_readiness.py     pre-write readiness checks
    path_safety.py         resolve_device_path + UnsafeDevicePathError
    storage_safety.py      file-size limits and allocation-size checks
    filesystem.py          filesystem type detection
    filesystem_profile.py  FilesystemProfile detection (case, separators…)
    durability.py          durable unlink / fsync behaviour
    eject.py               safe eject per OS
    metadata_write.py      SysInfo write safety
    recovery.py            read-only Linux mount facts + repair command plans
    dump.py                device database dumps
    linux_integration.py   Linux mount/udev integration helpers
  sync/
    __init__.py            docstring only — NO re-exports
    _db_io.py              read_existing_database / write_database /
                           verify_written_database / delete_playcounts_files
    _track_conversion.py   source dict → TrackInfo mapping
    _playlist_builder.py   build playlists + evaluate smart playlists
    spl_evaluator.py       smart playlist rule evaluation (spl_update*)
    ipod_track_paths.py    expected_ipod_track_file_path
    path_identity.py       path identity across case/separator differences
```

`podsync.sync.__init__` exports **nothing** — always import the concrete
submodule.

## 3. Chapters

| Chapter | Covers |
|---|---|
| `02-itunesdb-parser.md` | reading iTunesDB + Play Counts + forensics |
| `03-itunesdb-shared.md` | canonical layouts, constants, shared helpers |
| `04-itunesdb-writer.md` | writing iTunesDB, hashes 58/72 (AB refused), checksums |
| `05-artworkdb.md` | ArtworkDB + `.ithmb` codecs and pipelines |
| `06-device-identity.md` | discovery, models, capabilities, virtual devices |
| `07-device-safety.md` | write guard, path/storage safety, fs, eject |
| `08-sync.md` | read/convert/build/evaluate/write orchestration |

Chapters are written to be self-contained; where two chapters show the
same field table they are identical — when in doubt prefer chapter 03
for layout constants of iTunesDB chunks.

## 4. Dependencies and runtime

* Python 3.11+.
* Allowed third-party packages: **pycryptodome** (AES for hash 72),
  **pillow** (image work in the artwork pipeline), **pytest** (tests).
  Everything else must be stdlib.
* Platform code (Windows/macOS/Linux) must guard platform-only and
  optional imports (libusb/pyusb, IOKit) so that importing `podsync.device`
  works on any platform without extra packages.
* **mutagen** is an *optional* runtime package: `art_extractor` imports it
  lazily to read covers embedded in audio files (chapter 05 §11). It is not
  installed in this room, so only image files and folder art are testable
  here; with mutagen installed (as in the host application) embedded MP3/MP4
  covers work too. Nothing else may depend on it.
* No network access anywhere in the package.

## 5. Out of scope (do not implement)

These exist in the surrounding ecosystem but are deliberately absent from
podsync. Call sites must simply not exist, or — where the surrounding
logic implies an entry point — raise a clear `NotImplementedError`/
`RuntimeError` explaining the feature is unsupported:

1. **SQLite databases** for iPod nano 5G–7G (`write_sqlite_databases`
   and everything around it). Only the classic iTunesDB file family is
   written. `write_database` on such a device must fail with a clear
   unsupported-device error.
2. **`pc_track_to_info`** — conversion from a PC-library track record
   (the PC library scanner itself is not part of podsync).
3. **`commit_playcounts_if_needed` / `_commit_playcounts_guarded`** —
   committing play counts as a separate step. `playcounts.py` parsing
   and merging during a full read stays in scope.
4. **The ffmpeg/transcoder branch inside `art_extractor`** — cover
   extraction from audio files, folder images and the embedded `covr` of
   m4v/mp4/mov video stays; nothing grabs a video frame or shells out to
   a transcoder. A video without an embedded cover gets no artwork
   (`extract_art` returns `None`, never raises).
5. **Database hash “AB”** (iPod nano 6G/7G). `hashab.compute_hashab` /
   `write_hashab` raise `NotImplementedError`, and a write for such a
   device fails with a clear error (chapter 04 §6.4, §7 step 9). No WASM
   module or `wasmtime` is used anywhere.
6. **Recording identification on the iPod** — no SysInfo "authority"
   file, no background re-validation thread, no caching of live
   SysInfoExtended on the device; identification only reads (chapter 06
   §18). Device icons/colours (`images`) are not part of podsync.
7. **Everything beyond the file list in §2** — a GUI, an application
   layer, podcasts, photos, scrobbling, backups UI, fingerprinting,
   transcode caches, sync review, settings, an updater, `.ithmb` photo
   browsing, iTunes integration over COM, MTP, iPod Touch/iPhone,
   iTunesDB-compatible `iTunesPrefs` writing, `iTunesSD`.

## 6. Conventions

* **Tests are the acceptance criteria.** Module paths, names, signatures
  and behavior that tests rely on are mandatory.
* Exceptions: use the hierarchy in `podsync.itunesdb_parser.exceptions`
  for parse failures; the safety modules define their own precise
  error types (`UnsafeDevicePathError`, `DeviceWriteSafetyError`, …)
  as chapters 06/07 specify.
* All file formats little-endian unless a chapter states otherwise.
* Pure functions where possible; anything touching "the current device"
  uses the explicit state documented in chapter 06.
* Keep every module importable without side effects (no file/network
  access at import time).
