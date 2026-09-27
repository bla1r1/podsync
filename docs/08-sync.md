# 08 — `podsync.sync`: read → convert → build → evaluate → write → verify

This chapter specifies the seven files of the `podsync.sync` package: the
orchestration layer that reads an existing iPod database, converts track
rows into writer records, rebuilds playlists (including smart-playlist
rule evaluation), writes the database back, verifies the committed bytes
by reparsing them, and clears device-generated sync state afterwards.

Everything this chapter depends on from other packages (`podsync.device`,
`podsync.itunesdb_parser`, `podsync.itunesdb_shared`,
`podsync.itunesdb_writer`) is specified in chapters 02–07; this chapter
names those functions exactly and says *when* they are called, but does
not restate their internals except where the mapping performed by
`podsync.sync` itself requires it.

---

## 1. Module map, import rules, exclusions

### 1.1 Files and public surface

| Module path | Role | Public names (must exist) |
|---|---|---|
| `podsync.sync` | package marker, docstring only | *(nothing)* |
| `podsync.sync._db_io` | database read/write/verify/cleanup | `DatabaseVerificationError`, `read_existing_database`, `verify_written_database`, `write_database`, `delete_playcounts_files`, `commit_playcounts_if_needed`, private `_database_media_path_key`, `_commit_playcounts_guarded` |
| `podsync.sync._track_conversion` | parsed dict ⇄ `TrackInfo` mapping, eval-dict projection | `ipod_filetype_for_extension`, `track_dict_to_info`, `trackinfo_to_eval_dict` |
| `podsync.sync._playlist_builder` | dataset 2/3/5 playlist construction + SPL evaluation hook | `build_and_evaluate_playlists`, `sort_tracks_by_order`, `sort_trackinfos_by_order`, `decode_raw_blob` |
| `podsync.sync.spl_evaluator` | smart-playlist rule engine | `spl_update`, `spl_update_all`, `spl_update_from_parsed`, `eval_rule` |
| `podsync.sync.ipod_track_paths` | device `Location` string ⇄ on-disk path | `CachedIpodMusicPathResolver`, `expected_ipod_track_file_path`, `existing_ipod_track_file_path`, `ipod_location_from_file_path` |
| `podsync.sync.path_identity` | path identity / scalar coercion helpers | `stable_path_key`, `coerce_int` |

`podsync.sync.__init__.py` is **docstring-only**: it defines no names,
imports no submodules, and re-exports nothing. `from podsync.sync import
read_existing_database` must fail; callers always import the concrete
submodule (`from podsync.sync._db_io import read_existing_database`).
Importing `podsync.sync` must have no side effects beyond executing that
docstring.

### 1.2 Import style (test-visible)

Some names must be resolvable at *call time* through the module or
package object so tests can monkeypatch them. The required style:

| Call site | Required import style | Patchable target |
|---|---|---|
| `_db_io.write_database` → writer | `from podsync.itunesdb_writer import write_itunesdb` **inside the function body** | `podsync.itunesdb_writer.write_itunesdb` |
| `_db_io.write_database` → device lookup | `from podsync.device import get_current_device_for_path` inside the body | `podsync.device.get_current_device_for_path` |
| `_db_io.read_existing_database` → parser/extraction/time/OTG/artwork/playcounts | all imports **inside the function body**, from the *defining package*: `podsync.itunesdb_parser.parse_itunesdb` (i.e. `from podsync.itunesdb_parser import parse_itunesdb`), `from podsync.itunesdb_parser.playcounts import parse_playcounts, merge_playcounts`, `from podsync.itunesdb_parser.artwork_links import hydrate_track_artwork_refs`, `from podsync.itunesdb_parser.otg import load_otg_playlists`, `from podsync.itunesdb_shared.extraction import …`, `from podsync.itunesdb_shared.field_base import filetype_to_string`, `from podsync.itunesdb_shared.device_time import …`, `from podsync.device import resolve_itdb_path` | the package-level attributes shown above |
| `_db_io.verify_written_database` → `read_existing_database` and `write_database` → `verify_written_database` | **module-global** lookup (plain name call; both are module-level functions) | `podsync.sync._db_io.read_existing_database`, `podsync.sync._db_io.verify_written_database` |
| `_db_io.delete_playcounts_files` → unlink | `durable_unlink` bound at **module top level** (`from podsync.device.durability import durable_unlink`) | `podsync.sync._db_io.durable_unlink` |
| `_db_io.verify_written_database` → filesystem/path helpers | `from podsync.device.filesystem import detect_filesystem_type` and `from .ipod_track_paths import expected_ipod_track_file_path` inside the body | — |
| `_playlist_builder.build_and_evaluate_playlists` → eval projection + engine | `from ._track_conversion import trackinfo_to_eval_dict` and `from .spl_evaluator import spl_update` **inside the function body** | `podsync.sync.spl_evaluator.spl_update`, `podsync.sync._track_conversion.trackinfo_to_eval_dict` |

Top-level imports per module (needed for annotations and constants):

| Module | Top-level imports |
|---|---|
| `_db_io` | `logging`, `os`, `struct`, `pathlib.Path`, `collections.abc.Callable`, `podsync.device.durability.durable_unlink`, `podsync.device.path_safety` (`UnsafeDevicePathError`, `resolve_device_path`), `podsync.device.write_guard.DeviceWriteSafetyError`, `podsync.itunesdb_writer.mhit_writer.TrackInfo`, `podsync.itunesdb_writer.mhyp_writer.PlaylistInfo` |
| `_track_conversion` | `pathlib.Path`, `podsync.itunesdb_writer.mhit_writer.TrackInfo` |
| `_playlist_builder` | `logging`, `base64`, `podsync.itunesdb_shared.constants.MEDIA_TYPE_PODCAST`, `podsync.itunesdb_shared.device_time.DeviceTimeContext`, `podsync.itunesdb_shared.playlist_hierarchy.reconcile_playlist_hierarchy`, `podsync.itunesdb_shared.playlist_properties` (`playlist_description_from_row`, `playlist_property_raw_body_for_write`), `podsync.itunesdb_writer.mhit_writer.TrackInfo`, `podsync.itunesdb_writer.mhod_spl_writer` (`prefs_from_parsed`, `rules_from_parsed`), `podsync.itunesdb_writer.mhyp_writer` (`PlaylistInfo`, `PlaylistItemMeta`), `from .path_identity import coerce_int, stable_path_key` |
| `spl_evaluator` | `random`, `time`, `podsync.itunesdb_shared.device_time` (`DeviceTimeContext`, `current_device_time_context`), `podsync.itunesdb_shared.field_base.MAC_EPOCH_OFFSET`, `podsync.itunesdb_shared.mhod_defs` (all `SPL_*` names used below), `podsync.itunesdb_writer.mhod_spl_writer` (`RuleGroup`, `SmartPlaylistPrefs`, `SmartPlaylistRule`, `SmartPlaylistRules`) |
| `ipod_track_paths` | `os`, `stat`, `pathlib.Path`, `collections.abc.Mapping`, `typing.Any`, `podsync.device.path_safety` (`UnsafeDevicePathError`, `resolve_device_path`) |
| `path_identity` | `os`, `pathlib.Path` |

No module in `podsync.sync` may import `podsync.gui`,
`podsync.application`, `podsync.sync.transcoder`,
`podsync.sync.quick_writes`, `podsync.sync.mapping`,
`podsync.sync.contracts`, `podsync.sync.pc_library`,
or any application-layer package (§9). (`podsync.itdb.sqlite` — the SQLite
database writer, §7.3.3 — is a real module; it is `podsync.sqlitedb_writer`
under its original spec name, not one of the excluded ones above.)

### 1.3 Scope exclusions (explicit)

| Excluded item | Required behavior in podsync |
|---|---|
| `pc_track_to_info` (PC-library track → `TrackInfo`) | Absent from `_track_conversion`. The name must not exist (`AttributeError` on `podsync.sync._track_conversion.pc_track_to_info`); no PC-library/transcoder imports. |
| ~~`commit_playcounts_if_needed` and `_commit_playcounts_guarded`~~ | **Implemented** as `podsync.library.database.commit_playcounts_if_needed` (with a private `_commit_playcounts_guarded` helper) — see §7.6. Play-count *merging during a read* (§7.2.6) is what it rebuilds from; it is not a separate accounting path. |
| `sync.quick_writes`, `sync.mapping`, `sync.contracts`, `sync.transcoder`, `sync.pc_library` | Modules do not exist; any reference raises a clear `ImportError`/`ModuleNotFoundError`. (Reference tests that exercise `write_cached_itunesdb` etc. are out of scope; only their `build_and_evaluate_playlists` / `track_dict_to_info` cases apply.) |
| GUI / application / podcasts / scrobbling layers | Not present anywhere in `podsync.sync`. |

---

## 2. `podsync.sync.path_identity`

The identity rules every other module uses when comparing source paths
(scan results, playlist items, mapping hints, parsed rows).

### 2.1 API

```python
def stable_path_key(path: str | os.PathLike[str]) -> str: ...
def coerce_int(value: object, default: int = 0) -> int: ...
```

### 2.2 `stable_path_key` — what identifies a path

Algorithm:

1. `Path(path).expanduser()` — `~` is expanded first.
2. `os.path.normcase(str(expanded.resolve()))` — `resolve()` makes the
   path absolute, normalizes `.`/`..` and separator runs, and resolves
   symlinks (best effort); `normcase` then folds the path to the
   platform's canonical case/separator form.
3. If `resolve()` raises `OSError` (broken symlink chains, permissions),
   fall back to `os.path.normcase(os.path.abspath(os.fspath(expanded)))`.

Consequences the implementation must preserve:

* On **Windows**, `normcase` lowercases and converts `/` to `\`, so
  `C:\Lib\A.mp3`, `c:/lib/a.mp3`, and `\\?\...`-free variants of the same
  file produce one key → case and separator differences do **not**
  identify different files.
* On **POSIX**, `normcase` is the identity function: case differences
  remain significant (`/Lib/A.mp3` ≠ `/lib/a.mp3`), because the mounted
  iPod filesystem may be case-sensitive.
* The key is always an absolute path string; relative inputs are
  resolved against the current working directory (via `abspath`/`resolve`).

### 2.3 `coerce_int`

| Input | Result |
|---|---|
| `None` | `default` (0 unless overridden) |
| any type outside `str`, `bytes`, `bytearray`, `int`, `float` (dicts, lists, objects, `Path`, …) | `default` — never attempt `int(obj)` on arbitrary objects |
| `int` (incl. `bool`: `True` → 1) | the value |
| `float` | `int(value)` truncated toward zero; NaN → `default` (`ValueError`); ±infinity is **not** caught — `int()` raises `OverflowError`, which propagates |
| `str` / `bytes` / `bytearray` parseable by `int(...)` (e.g. `"12"`, `b"-3"`) | parsed integer |
| `str` that does not parse (`"3.5"`, `""`, `"abc"`) | `default` (catch `ValueError`; also `TypeError`) |

Used throughout the playlist builder for `track_id`, `db_track_id`,
`playlist_id`, `mhsd5_type`, `parent_folder_playlist_id`,
`phase_game_flag`.

---

## 3. `podsync.sync.ipod_track_paths`

Single place that turns iTunesDB `Location` strings into real files under
the iPod and back, so planning, integrity checks, exports, and execution
all resolve the same device file.

### 3.1 API

```python
def expected_ipod_track_file_path(ipod_root: str | Path,
                                  track_or_location: Mapping | str | Path | None) -> Path | None
def existing_ipod_track_file_path(ipod_root: str | Path,
                                  track_or_location: Mapping | str | Path | None, *,
                                  allow_music_filename_fallback: bool = False) -> Path | None
def ipod_location_from_file_path(ipod_root: str | Path, file_path: str | Path) -> str
class CachedIpodMusicPathResolver:
    def __init__(self, ipod_root: str | Path) -> None
    def existing_regular_file(self, track_or_location) -> tuple[Path, os.stat_result] | None
```

Constant: `_TRACKS_SUBTREE = Path("iPod_Control") / "Music"` — every
resolved path must live inside this subtree (enforced through
`podsync.device.path_safety.resolve_device_path`, chapter 07).

`track_or_location` may be a mapping (key `"Location"` first, then
`"location"`), a `str`, a `Path`, or `None`; the value is coerced with
`str(raw or "").strip()`; `None`/empty → treated as “no location”.

### 3.2 Location grammar — `_device_relative_track_location(location)`

Input normalization to the relative form
`iPod_Control/Music/<rest…>` (returned as a `/`-joined string), or
`None` when the location cannot be an on-device music file. Apply the
rules **in order**:

| # | Rule | Result on match |
|---|---|---|
| 1 | empty string, or contains `\x00` | `None` |
| 2 | replace `\` with `/` (unified form) | continue |
| 3 | starts with `//` (UNC-ish) | `None` |
| 4 | Windows-drive form: length ≥ 2, first char alphabetic, second `:` | flag `is_windows_drive_path = True` (this is checked on the unified string; `X:` at index 0/1) |
| 5 | `:` present and **not** a Windows drive path | replace every `:` with `/` (colon locations `:iPod_Control:Music:F00:A.mp3` and POSIX paths with colons) |
| 6 | split on `/`; find the **first** component whose `.lower() == "ipod_control"` | if found, discard everything before it and continue from the marker; if not found: a string that starts with `/` **or** is a Windows drive path → `None` (external absolute path with no iPod marker); otherwise use the whole path as the candidate (relative form) |
| 7 | candidate must have ≥ 2 components and `candidate[:2]` lower-equal to `["ipod_control", "music"]` | otherwise `None` |
| 8 | success | return `"iPod_Control/Music/" + "/".join(candidate[2:])` — the two leading components are emitted with canonical casing; **all remaining components keep their original case and text verbatim** (no sanitization, no case folding) |

Notes:

* A colon location with `..` segments (e.g.
  `:iPod_Control:Music:F00:..:..:outside.mp3`) passes this grammar but is
  rejected later by `resolve_device_path` (it escapes the
  `iPod_Control/Music` subtree) → `None`.
* `_strip_file_uri` runs first in the public functions: a `file://` URI
  is parsed with `urllib.parse.urlparse` and its path is `unquote`d and
  stripped; non-`file://` strings pass through unchanged.

### 3.3 `expected_ipod_track_file_path`

| Step | Behavior |
|---|---|
| 1 | falsy `ipod_root` or empty coerced location → `None` |
| 2 | strip `file://` URI → empty → `None` |
| 3 | grammar (§3.2) → `None` → return `None` |
| 4 | `resolve_device_path(root, relative_location, allowed_subtree=iPod_Control/Music)` |
| 5 | `UnsafeDevicePathError` (traversal, symlink escape, outside subtree) → `None`; otherwise return the `Path` |

The returned path **need not exist** — a missing file is still
meaningful for integrity checks, removals, and orphan comparison.
There is no per-device path template: the only layout assumed is
`<root>/iPod_Control/Music/<folder>/<file…>`, and no filename
sanitization happens here (the module parses locations; it does not
generate or rewrite names).

Worked expectations (all must hold):

| Location | Result |
|---|---|
| `":iPod_Control:Music:F00:GONE.mp3"` | `<root>/iPod_Control/Music/F00/GONE.mp3` |
| `r"X:\iPod_Control\Music\F01\GONE.m4a"` | `<root>/iPod_Control/Music/F01/GONE.m4a` |
| `r"C:\Users\Someone\Music\Song.mp3"` (no marker) | `None` |
| `":iPod_Control:Music:F00:..:..:outside.mp3"` | `None` (escapes subtree) |
| absolute host file `/tmp/…/Song.mp3` (no marker) | `None` |
| location containing NUL | `None` |
| `Music/F00` is a symlink pointing outside the iPod | `None` (detected by `resolve_device_path`) |
| `file:///Volumes/IPOD/iPod_Control/Music/F00/Song.mp3` | `<root>/iPod_Control/Music/F00/Song.mp3` |

### 3.4 `existing_ipod_track_file_path`

1. Falsy root → `None`.
2. `expected = expected_ipod_track_file_path(...)`; if `expected` is not
   `None` **and** `expected.is_file()` → return it.
3. If `allow_music_filename_fallback` is `False` → `None`.
4. Filename selection: `expected.name` when `expected` is not `None`;
   otherwise derive from the location's grammar-relative path
   (`Path(rel).name`); empty → `None`.
5. `_find_music_file_by_name(root, filename)`:
   * resolve the `iPod_Control/Music` root via `resolve_device_path`
     (failure or non-directory → `None`);
   * iterate `music_root.rglob("*")` in traversal order; every candidate
     is re-checked with `resolve_device_path(root, candidate.relative_to(root),
     allowed_subtree=Music)` (skip on `UnsafeDevicePathError`/`ValueError`)
     and must be a regular file;
   * first file whose name matches `filename.lower()` (case-insensitive
     **full name**) is returned immediately;
   * otherwise remember the **first** file whose *stem* matches
     `filename.stem.lower()` (case-insensitive, different extension
     allowed) and return it after the scan; no match → `None`.
   * Return value is additionally required to `is_file()`.

Fallback example: location `:iPod_Control:Music:F00:REALNAME.mp3` with
the real file at `Music/F37/REALNAME.m4a` and fallback enabled → returns
the `F37` file.

### 3.5 `ipod_location_from_file_path`

```python
ipod_location_from_file_path(ipod_root, file_path) -> ":iPod_Control:Music:…"
```

1. `root = Path(ipod_root).resolve(strict=False)`; a relative
   `file_path` is joined under `root`.
2. `path.resolve(strict=False).relative_to(root)` — on `ValueError`
   (path outside the iPod root) raise `UnsafeDevicePathError`
   `f"Track path is outside the iPod music directory: {file_path!s}"`.
3. The relative path is passed through
   `resolve_device_path(root, relative, allowed_subtree=Music)`; the
   result must still be relative to `root` (a path inside the iPod but
   outside `iPod_Control/Music` raises `UnsafeDevicePathError` from
   `resolve_device_path`, chapter 07).
4. Return `":" + ":".join(relative.parts)` — canonical
   `iPod_Control`/`Music` casing from the resolved relative parts,
   colon separators, leading colon.

Example: `Music/F00/Song.mp3` under the root →
`:iPod_Control:Music:F00:Song.mp3`; `tmp/host-song.mp3` →
`UnsafeDevicePathError` with the message above.

### 3.6 `CachedIpodMusicPathResolver`

Batch-oriented variant for the *conventional* shape only.

* `__init__(ipod_root)` stores the root and an empty per-folder cache
  `self._folders: dict[str, Path | None]`.
* `existing_regular_file(track_or_location)`:
  1. coerce location and strip `file://`, run the grammar;
  2. the relative location must split into **exactly 4 parts** with
     `parts[:2] == ("iPod_Control", "Music")` and no part that is empty,
     `"."`, `".."`, or contains `:` — anything else (deeper nesting,
     shallower paths, absolute forms) → `None` (callers that need those
     shapes use `expected_ipod_track_file_path` instead; this class does
     not internally fall back);
  3. folder part: look up `self._folders`; on miss call
     `resolve_device_path(root, iPod_Control/Music/<folder>,
     allowed_subtree=Music)` and cache the resulting `Path`, or cache
     `None` if it raised `UnsafeDevicePathError` (negative caching —
     the safety check runs **once per folder name** per resolver
     instance);
  4. cached `None` → `None`;
  5. `candidate = folder / file_name`; `lstat()` (`OSError` → `None`);
  6. require `stat.S_ISREG(metadata.st_mode)` (directories, symlinks to
     directories, devices rejected) → return `(candidate, metadata)`;
     otherwise `None`.

Because the leaf is joined from an already-validated folder plus a
colon-free, dot-free filename, only the file leaf can still fail
(`lstat` / type check).

---

## 4. `podsync.sync._track_conversion`

Pure data-mapping functions; no side effects, no I/O.

### 4.1 `ipod_filetype_for_extension(extension) -> str`

`ext = extension.casefold().lstrip(".")` (any number of leading dots),
then:

| `ext` in … | result |
|---|---|
| `{"m4a", "aac", "alac"}` | `"m4a"` |
| `{"m4v", "mov"}` | `"m4v"` (iTunesDB has no `MOV` code; QuickTime movies are video payloads) |
| `{"mp3", "mp4", "wav"}` | `ext` unchanged |
| anything else | `ext`, or `"mp3"` when `ext` is empty |

### 4.2 Filetype resolution used by `track_dict_to_info`

Input: `raw = t.get("filetype", "MP3")`. In a parsed dict this key holds
the MHIT fourcc already turned into text by `filetype_to_string`
(`"MP3"`, `"M4A"`, `"M4P"`, `"M4B"`, `"M4V"`, `"MP4"`, `"WAV"`, `"AIFF"`,
`"AAC"`, …); the MHOD 6 description lives under the separate key
`"Filetype"` (capital F). Resolution, first rule that applies:

1. `raw` is not a `str` → treat as `""`.
2. **Exact fourcc**: `raw.strip().casefold()` is a key of `FILETYPE_CODES`
   (chapter 03 §1.9: `mp3, m4a, m4p, m4b, m4v, mp4, wav, aif, aiff, aac`)
   → that key. This keeps audiobooks (`M4B`) and protected files (`M4P`)
   intact across a read → write cycle.
3. Otherwise scan `_FILETYPE_MAP` below.

> Step 2 is required: the fourcc strings `M4B`/`M4P` contain none of the
> needles below, so a substring scan alone would turn audiobooks and
> protected files into `mp3` on every rewrite (MP3 fourcc, `mp3_flag = 1`,
> audiobook `audio_format_flag` lost).

`_FILETYPE_MAP` is a list of `(needle, code)` pairs scanned **in order,
first substring match wins**, using case-sensitive `needle in raw` (it
also covers description-style strings and `MOV`, which has no fourcc):

| order | needle | code |
|---|---|---|
| 1 | `AAC` | `m4a` |
| 2 | `M4A` | `m4a` |
| 3 | `Lossless` | `m4a` |
| 4 | `Protected` | `m4p` |
| 5 | `Audiobook` | `m4b` |
| 6 | `WAV` | `wav` |
| 7 | `AIFF` | `aiff` |
| 8 | `M4V` | `m4v` |
| 9 | `MP4` | `mp4` |
| 10 | `MOV` | `m4v` |

No match → `"mp3"`. Worked examples: `"M4B"` → `m4b`; `"M4P"` → `m4p`;
`"AAC"` → `aac`; `"MP3"` → `mp3` (all step 2); `"MOV"` → `m4v`;
`"Protected AAC audio file"` → `m4a` (needle 1 `AAC` matches before
needle 4 `Protected`); `"Protected MPEG audio file"` → `m4p`;
`"MPEG audio file"` → `mp3` (no needle matches).

### 4.3 `track_dict_to_info(t: dict) -> TrackInfo`

Full mapping — parsed track dict key → `TrackInfo` constructor argument.
Every row is mandatory behavior (defaults shown are the exact ones):

| `TrackInfo` arg | source expression |
|---|---|
| `title` | `t.get("Title", "Unknown")` |
| `location` | `t.get("Location", "")` |
| `size` | `t.get("size", 0)` |
| `length` | `t.get("length", 0)` |
| `filetype` | resolution of `t.get("filetype", "MP3")` per §4.2 |
| `bitrate` | `t.get("bitrate", 0)` |
| `sample_rate` | `t.get("sample_rate_1", 44100)` (parser already converted 16.16 → Hz) |
| `vbr` | `bool(t.get("vbr_flag", 0))` |
| `artist` | `t.get("Artist")` |
| `album` | `t.get("Album")` |
| `album_artist` | `t.get("Album Artist")` |
| `genre` | `t.get("Genre")` |
| `composer` | `t.get("Composer")` |
| `comment` | `t.get("Comment")` |
| `grouping` | `t.get("Grouping")` |
| `year` | `t.get("year", 0)` |
| `track_number` | `t.get("track_number", 0)` |
| `total_tracks` | `t.get("total_tracks", 0)` |
| `disc_number` | `t.get("disc_number", 1)` |
| `total_discs` | `t.get("total_discs", 1)` |
| `bpm` | `t.get("bpm", 0)` |
| `compilation_flag` | `bool(t.get("compilation_flag", t.get("compilation", 0)))` |
| `skip_when_shuffling` | `bool(t.get("skip_when_shuffling", 0))` |
| `remember_position` | `bool(t.get("remember_position", 0))` |
| `rating` | `t.get("rating", 0)` |
| `play_count` | `t.get("play_count_1", 0)` — cumulative count, incl. deltas already folded at load |
| `play_count_2` | `t.get("play_count_2", 0)` — durable pending-scrobble queue, independent of the current Play Counts delta |
| `skip_count` | `t.get("skip_count", 0)` |
| `volume` | `t.get("volume", 0)` |
| `start_time` | `t.get("start_time", 0)` |
| `stop_time` | `t.get("stop_time", 0)` |
| `sound_check` | `t.get("sound_check", 0)` |
| `bookmark_time` | `t.get("bookmark_time", 0)` |
| `checked_flag` | `t.get("checked_flag", t.get("checked", 0))` |
| `gapless_data` | `t.get("gapless_audio_payload_size", 0)` |
| `gapless_track_flag` | `t.get("gapless_track_flag", 0)` |
| `gapless_album_flag` | `t.get("gapless_album_flag", 0)` |
| `pregap` | `t.get("pregap", 0)` |
| `postgap` | `t.get("postgap", 0)` |
| `sample_count` | `t.get("sample_count", 0)` |
| `encoder_flag` | `t.get("encoder", 0)` |
| `explicit_flag` | `t.get("explicit_flag", 0)` |
| `purchased_aac_flag` | `t.get("purchased_aac_flag", 0)` |
| `has_lyrics` | `bool(t.get("lyrics_flag", 0))` |
| `lyrics` | `t.get("Lyrics")` |
| `eq_setting` | `t.get("eq_setting")` |
| `date_added` | `t.get("date_added", 0)` |
| `date_released` | `t.get("date_released", 0)` |
| `last_played` | `t.get("last_played", 0)` |
| `last_skipped` | `t.get("last_skipped", 0)` |
| `last_modified` | `t.get("last_modified", 0)` |
| `date_added_to_itunes` | `t.get("date_added_to_itunes", 0)` |
| `db_track_id` | `t.get("db_track_id", t.get("db_id", 0))` |
| `media_type` | `t.get("media_type", 1)` |
| `movie_file_flag` | `t.get("movie_flag", 0)` |
| `source_path` | `t.get("source_path") or t.get("Source Path")` |
| `source_relative_path` | `t.get("source_relative_path") or t.get("Source Relative Path")` |
| `season_number` | `t.get("season_number", 0)` |
| `episode_number` | `t.get("episode_number", 0)` |
| `artwork_count` | `t.get("artwork_count", 0)` |
| `artwork_size` | `t.get("artwork_size", 0)` |
| `mhii_link` | `t.get("artwork_id_ref", 0)` |
| `sort_artist` | `t.get("Sort Artist")` |
| `sort_name` | `t.get("Sort Title") or t.get("Sort Name")` |
| `sort_album` | `t.get("Sort Album")` |
| `sort_album_artist` | `t.get("Sort Album Artist")` |
| `sort_composer` | `t.get("Sort Composer")` |
| `filetype_desc` | `t.get("Filetype")` — the MHOD 6 description (e.g. `"MPEG audio file"`), `None` when absent. (Not `t.get("filetype")`: that key holds the fourcc, and using it would replace the description — e.g. the localized `"Аудиофайл MPEG"` seen on a real iPod — with `"MP3"` on every rewrite.) |
| `show_name` | `t.get("Show")` |
| `episode_id` | `t.get("Episode")` |
| `description` | `t.get("Description Text")` |
| `subtitle` | `t.get("Subtitle")` |
| `network_name` | `t.get("TV Network")` |
| `sort_show` | `t.get("Sort Show")` |
| `show_locale` | `t.get("Show Locale")` |
| `keywords` | `t.get("Track Keywords")` |
| `podcast_enclosure_url` | `t.get("Podcast Enclosure URL")` |
| `podcast_rss_url` | `t.get("Podcast RSS URL")` |
| `category` | `t.get("Category")` |
| `played_mark` | `t.get("not_played_flag", -1)` |
| `podcast_flag` | `t.get("use_podcast_now_playing_flag", 0)` |
| `user_id` | `t.get("user_id", 0)` |
| `app_rating` | `t.get("app_rating", 0)` |
| `mpeg_audio_type` | `t.get("mpeg_audio_type", t.get("unk144", 0))` |
| `store_track_id` | `t.get("store_track_id", 0)` |
| `store_encoder_version` | `t.get("store_encoder_version", 0)` |
| `store_artist_id` | `t.get("store_artist_id", 0)` |
| `store_album_id` | `t.get("store_album_id", 0)` |
| `store_content_flag` | `t.get("store_content_flag", 0)` |
| `album_id` | `t.get("album_id", 0)` |
| `artist_id` | `t.get("artist_id_ref", t.get("artist_id", 0))` |
| `composer_id` | `t.get("composer_id", 0)` |
| `chapter_data` | `t.get("chapter_data")` |

`track_id` is left at its default (0); the writer assigns it during
write. String keys absent from the dict yield `None` (no `""`
substitution). Examples that must hold: a dict with
`media_type=MEDIA_TYPE_PODCAST, use_podcast_now_playing_flag=1,
skip_when_shuffling=1, remember_position=1` converts to
`media_type == MEDIA_TYPE_PODCAST`, `podcast_flag == 1`,
`skip_when_shuffling is True`, `remember_position is True`;
`{"filetype": "MOV"}` converts to `filetype == "m4v"`.

### 4.4 `trackinfo_to_eval_dict(t: TrackInfo) -> dict`

Projection consumed by `spl_evaluator` (keys must satisfy the accessors
of §5.2). Exact table:

| key | value |
|---|---|
| `track_id` | `t.db_track_id` (so `spl_update` returns db track ids on this path) |
| `Title` | `t.title or ""` |
| `Album` | `t.album or ""` |
| `Artist` | `t.artist or ""` |
| `Genre` | `t.genre or ""` |
| `filetype` | `t.filetype_desc or t.filetype or ""` |
| `Comment` | `t.comment or ""` |
| `Composer` | `t.composer or ""` |
| `Album Artist` | `t.album_artist or ""` |
| `Sort Title` | `t.sort_name or ""` |
| `Sort Album` | `t.sort_album or ""` |
| `Sort Artist` | `t.sort_artist or ""` |
| `Sort Album Artist` | `t.sort_album_artist or ""` |
| `Sort Composer` | `t.sort_composer or ""` |
| `Sort Show` | `t.sort_show or ""` |
| `Grouping` | `t.grouping or ""` |
| `bitrate` | `t.bitrate` |
| `sample_rate_1` | `t.sample_rate` |
| `year` | `t.year` |
| `track_number` | `t.track_number` |
| `size` | `t.size` |
| `length` | `t.length` |
| `play_count_1` | `t.play_count` |
| `disc_number` | `t.disc_number` |
| `rating` | `t.rating` |
| `bpm` | `t.bpm` |
| `skip_count` | `t.skip_count` |
| `date_added` | `t.date_added` |
| `last_modified` | `t.last_modified` |
| `last_played` | `t.last_played` |
| `last_skipped` | `t.last_skipped` |
| `compilation_flag` | `1 if t.compilation_flag else 0` |
| `has_artwork` | `bool(t.artwork_count or t.mhii_link)` (must be a real `bool`) |
| `artwork_count` | `t.artwork_count` |
| `artwork_id_ref` | `t.mhii_link` |
| `purchased_flag` | `t.purchased_aac_flag` |
| `media_type` | `t.media_type` |
| `location_kind` | always `1` (tracks being evaluated are local to this iPod; the Location rule uses bit 0 = “on this computer”) |
| `checked_flag` | `t.checked_flag` (0 = checked, 1 = unchecked in the parser convention) |
| `season_number` | `t.season_number` |
| `Show` | `t.show_name or ""` |
| `Description Text` | `t.description or ""` |
| `Category` | `t.category or ""` |
| `podcast_flag` | `t.podcast_flag` |

**Invariant (test-enforced):** the union of the values of
`SPL_HOST_STRING_FIELD_KEYS`, `SPL_HOST_INT_FIELD_KEYS`,
`SPL_HOST_DATE_FIELD_KEYS`, `SPL_HOST_BOOLEAN_FIELD_KEYS`, and
`SPL_HOST_BINARY_AND_FIELD_KEYS` (chapter 03) must be a subset of the
keys produced here. String fields default to `""`, never `None`.

---

## 5. `podsync.sync.spl_evaluator` — the smart-playlist engine

Evaluates parsed smart-playlist rules against a list of track dicts and
returns the matching track ids that should be written as MHIPs.

### 5.1 Inputs (dataclasses from `podsync.itunesdb_writer.mhod_spl_writer`, chapter 04)

| Type | Fields used by the evaluator |
|---|---|
| `SmartPlaylistPrefs` | `live_update`, `check_rules`, `check_limits`, `limit_type`, `limit_sort`, `limit_value`, `match_checked_only` |
| `SmartPlaylistRule` | `field_id`, `action_id`, `string_value`, `from_value`, `from_date`, `from_units`, `to_value`, `to_date`, `to_units` |
| `SmartPlaylistRules` | `conjunction` (`"AND"` / `"OR"`), `rules: list[SmartPlaylistRule \| RuleGroup]` |
| `RuleGroup` | `group: SmartPlaylistRules` (nesting wrapper) |

Track dicts are either parsed rows (chapter 02 output) or
`trackinfo_to_eval_dict` projections (§4.4).

### 5.2 Field accessors — `field_id` → track-dict key

Imported from `podsync.itunesdb_shared.mhod_defs` (chapter 03 owns
these constants; the table is repeated here because this module's
behavior *is* the table):

**String fields** (`SPL_HOST_STRING_FIELD_KEYS`) — value read with
`track.get(key, "")`; only `str` values are used, otherwise `""`; the
result is **case-folded** before comparison:

| id | name | key | id | name | key |
|---|---|---|---|---|---|
| 0x02 | Song Name | `Title` | 0x47 | Album Artist | `Album Artist` |
| 0x03 | Album | `Album` | 0x4E | Sort Song Name | `Sort Title` |
| 0x04 | Artist | `Artist` | 0x4F | Sort Album | `Sort Album` |
| 0x08 | Genre | `Genre` | 0x50 | Sort Artist | `Sort Artist` |
| 0x09 | Kind | `filetype` | 0x51 | Sort Album Artist | `Sort Album Artist` |
| 0x0E | Comment | `Comment` | 0x52 | Sort Composer | `Sort Composer` |
| 0x12 | Composer | `Composer` | 0x53 | Sort TV Show | `Sort Show` |
| 0x27 | Grouping | `Grouping` | 0x36 | Description | `Description Text` |
| 0x37 | Category | `Category` | 0x3E | TV Show | `Show` | 

**Integer fields** (`SPL_HOST_INT_FIELD_KEYS`, plus
`SPL_HOST_BINARY_AND_FIELD_KEYS` as fallback in the accessor) — value
used only if `isinstance(val, int)` (bools count), otherwise `0`;
missing key → `0`:

| id | name | key | id | name | key |
|---|---|---|---|---|---|
| 0x05 | Bit Rate | `bitrate` | 0x19 | Rating | `rating` |
| 0x06 | Sample Rate | `sample_rate_1` | 0x23 | BPM | `bpm` |
| 0x07 | Year | `year` | 0x39 | Podcast | `podcast_flag` |
| 0x0B | Track Number | `track_number` | 0x3C | Media Kind | `media_type` |
| 0x0C | Size | `size` | 0x3F | Season Number | `season_number` |
| 0x0D | Time | `length` | 0x44 | Skips | `skip_count` |
| 0x16 | Plays | `play_count_1` | 0x85 | Location | `location_kind` (binary-AND map) |
| 0x18 | Disc Number | `disc_number` | | | |

**Date fields** (`SPL_HOST_DATE_FIELD_KEYS`) — Unix timestamps, same
`isinstance(val, int)` rule, missing → `0`:

| id | name | key |
|---|---|---|
| 0x0A | Date Modified | `last_modified` |
| 0x10 | Date Added | `date_added` |
| 0x17 | Last Played | `last_played` |
| 0x45 | Last Skipped | `last_skipped` |

**Boolean accessor** (`_get_bool_value`) — three ids are handled
*before* consulting `SPL_HOST_BOOLEAN_FIELD_KEYS`:

| id | name | value |
|---|---|---|
| 0x1D | Checked | `track.get("checked_flag", 0) == 0` (polarity: “Checked is true” means *checked*) |
| 0x25 | Album Artwork | `bool(track.get("has_artwork") or track.get("artwork_count") or track.get("artwork_id_ref"))` |
| 0x29 | Purchased | `bool(track.get("purchased_flag") or track.get("Purchased"))` |
| 0x1F | Compilation | via `SPL_HOST_BOOLEAN_FIELD_KEYS` → `bool(track.get("compilation_flag", 0))`; unknown id → `False` |

**Playlist field** 0x28 is evaluated against
`playlist_lookup[rule.from_value]` (§5.7), never against a track key.

Unknown field id (not in `SPL_FIELD_TYPE_MAP`) → the rule never matches
(`False`).

### 5.3 Rule dispatch — `eval_rule(rule, track, playlist_lookup=None, time_context=None) -> bool`

1. `isinstance(rule, RuleGroup)` → evaluate `rule.group` as a container
   (§5.5) and return.
2. Special pre-dispatch: `field_id == 0x3C` **and**
   `action_id in (0x00000400, 0x02000400)` → binary-AND evaluation on
   the integer value of 0x3C (`media_type`), i.e. Media-Kind
   “includes/excludes” rules (0x3C is typed INT in the field-type map,
   so without this pre-check the binary actions would not match).
3. Otherwise look up `SPL_FIELD_TYPE_MAP[field_id]` (chapter 03) and
   dispatch:

| type | enum | evaluator |
|---|---|---|
| 1 | `SPLFT_STRING` | `_eval_string(casefolded value, rule)` |
| 2 | `SPLFT_INT` | `_eval_int(value, rule)` |
| 3 | `SPLFT_BOOLEAN` | `_eval_boolean(value, rule)` |
| 4 | `SPLFT_DATE` | `_eval_date(value, rule, time_context or current_device_time_context())` |
| 5 | `SPLFT_PLAYLIST` | `_eval_playlist(track, rule, playlist_lookup)` |
| 7 | `SPLFT_BINARY_AND` | `_eval_binary_and(value, rule)` |
| 6 / missing | unknown | `False` |

Type membership (needed to reason about behavior): STRING =
{0x02, 0x03, 0x04, 0x08, 0x09, 0x0E, 0x12, 0x27, 0x36, 0x37, 0x3E, 0x47,
0x4E, 0x4F, 0x50, 0x51, 0x52, 0x53, 0x59, 0x9F, 0xA0}; INT = {0x05,
0x06, 0x07, 0x0B, 0x0C, 0x0D, 0x16, 0x18, 0x19, 0x23, 0x39, 0x3C, 0x3F,
0x44, 0x5A, 0x86, 0x9A, 0x9C, 0xA1}; DATE = {0x0A, 0x10, 0x17, 0x45};
BOOLEAN = {0x1D, 0x1F, 0x25, 0x29}; PLAYLIST = {0x28}; BINARY_AND =
{0x85}.

### 5.4 Operator tables (`action_id` semantics)

Action ids are 32-bit bitmapped values (bits 24–25: 0x00 int/date,
0x01 string, 0x02 negated int/date, 0x03 negated string; low bits =
operator). Unknown action → `False`. `fv = rule.from_value`,
`tv = rule.to_value`.

**Strings** (both sides case-folded; `rule.string_value is None` →
`False`):

| action | meaning | test |
|---|---|---|
| 0x01000001 | is | `track == rule` |
| 0x03000001 | is not | `track != rule` |
| 0x01000002 | contains | `rule in track` |
| 0x03000002 | does not contain | `rule not in track` |
| 0x01000004 | begins with | `track.startswith(rule)` |
| 0x03000004 | does not begin with | `not startswith` |
| 0x01000008 | ends with | `track.endswith(rule)` |
| 0x03000008 | does not end with | `not endswith` |

**Integers:**

| action | meaning | test |
|---|---|---|
| 0x00000001 | is | `== fv` |
| 0x02000001 | is not | `!= fv` |
| 0x00000010 | is greater than | `> fv` |
| 0x02000010 | is not greater than | `<= fv` |
| 0x00000040 | is less than | `< fv` |
| 0x02000040 | is not less than | `>= fv` |
| 0x00000100 | is in the range | `min(fv,tv) <= v <= max(fv,tv)` (inclusive both ends) |
| 0x02000100 | is not in the range | `v < lo or v > hi` |

(Actions 0x00000020 / 0x00000080 and their negations are not evaluated
by the host engine → `False`.)

**Booleans:** 0x00000001 → “is true” = the value; 0x02000001 → “is
false” = `not value`; anything else → `False`.

**Binary AND:** 0x00000400 → `bool(value & fv)` (“includes”);
0x02000400 → `not bool(value & fv)` (“excludes”); else `False`.

**Playlist membership:** `playlist_lookup is None` → `False` for every
action. `member = playlist_lookup.get(rule.from_value, set())`,
`tid = track.get("track_id", 0)`:
0x00000001 → `tid in member`; 0x02000001 → `tid not in member`; else
`False`.

### 5.5 Group logic — `_eval_rule_container`

* Container = one `SmartPlaylistRules` (root or the `group` of a
  `RuleGroup`).
* `rules.conjunction.upper() == "OR"` → `any(child matches)` over
  `rules.rules`.
* Anything else (including `"AND"`) → `all(child matches)`.
* Children may be `SmartPlaylistRule` **or** `RuleGroup`; groups recurse
  into the same container rule → arbitrary nesting of AND/OR.
* Empty `rules.rules`: `any([])` = `False`, `all([])` = `True` — but see
  `spl_update`, which does not call the container at all when the rule
  list is empty.

### 5.6 Date evaluation and time contexts

**Absolute dates** (`_rule_date_to_unix(value, time_context)`):

1. `value = int(value or 0)`.
2. If `value >= MAC_EPOCH_OFFSET` (2_082_844_800 — the Mac-epoch
   seconds offset, from `podsync.itunesdb_shared.field_base`), the value
   is an iPod-local Mac timestamp:
   `(time_context or current_device_time_context()).mac_to_unix(value)`.
   The **device** time context therefore decides the interpretation —
   the same stored rule matches against a track under the device's
   timezone and fails under a different one.
3. Otherwise the value is already a Unix timestamp → returned unchanged
   (0 and negatives pass through).

Absolute actions (after decoding `fv`, `tv`):

| action | meaning | test |
|---|---|---|
| 0x00000001 | is | `fv <= v <= (tv or fv)` — a zero `to_value` collapses to the single point/day `fv` |
| 0x02000001 | is not | negation of the above |
| 0x00000010 | is after | `v > fv` |
| 0x02000010 | is not after | `v <= fv` |
| 0x00000040 | is before | `v < fv` |
| 0x02000040 | is not before | `v >= fv` |
| 0x00000100 | is in the range | `lo <= v <= hi` (min/max of `fv`,`tv`) |
| 0x02000100 | is not in the range | outside `[lo, hi]` |

**Relative dates are evaluated first, before any marker decoding** —
for actions 0x00000200 (“is in the last”) and 0x02000200 (“is not in the
last”) the value fields are *format sentinels*, not timestamps:

* `threshold = int(time.time()) + (rule.from_date * rule.from_units)`
  (`from_date` is the count and `from_units` the unit size in seconds;
  both are negative for a point in the past — e.g. “in the last 7 days”
  is `from_date = -1`, `from_units = 86400` with
  `from_value == to_value == SPL_DATE_IDENTIFIER` (0x2DAE2DAE2DAE2DAE)).
* 0x00000200 → `track_val > threshold`; 0x02000200 →
  `track_val <= threshold`.
* The `SPL_DATE_IDENTIFIER` markers must **never** be passed through
  `_rule_date_to_unix` for these actions.

Unit vocabulary (chapter 03): 1 s, 60 min, 3600 h, 86400 d, 604800 w,
2628000 ~month.

`DeviceTimeContext` comes from `podsync.itunesdb_shared.device_time`
(chapter 03): `mac_to_unix`, classmethods `utc()`, `fixed_offset()`,
`from_timezone_name()`; `current_device_time_context()` returns the
active context or UTC.

### 5.7 `eval_rule` and the single-playlist entry point

```python
def eval_rule(rule: SmartPlaylistRule | RuleGroup, track: dict,
              playlist_lookup: dict[int, set[int]] | None = None,
              time_context: DeviceTimeContext | None = None) -> bool
```

Dispatch per §5.3; group nodes recurse. This is the primitive both
`spl_update` and external callers use (per-rule testing).

### 5.8 `spl_update` — the main evaluator

```python
def spl_update(prefs: SmartPlaylistPrefs, rules: SmartPlaylistRules,
               tracks: list[dict],
               playlist_lookup: dict[int, set[int]] | None = None,
               time_context: DeviceTimeContext | None = None) -> list[int]
```

Three phases:

**Phase 1 — selection** (input order preserved):

1. If `prefs.match_checked_only` and `track.get("checked_flag", 0) != 0`
   → skip the track (only “checked” tracks are eligible).
2. If `prefs.check_rules` **and** `rules.rules` is non-empty → keep the
   track only when `_eval_rule_container(rules, track, playlist_lookup,
   time_context)` is true.
3. Otherwise (`check_rules` false, or no rules) → **every remaining
   track is selected** (rules disabled means “match all”).
4. Empty selection → return `[]` (limits phase is skipped).

**Phase 2 — limits** (only when `prefs.check_limits`):

1. Sort `selected` per `prefs.limit_sort` (§5.9). `limit_sort == 0x02`
   (random) → `random.shuffle(selected)` in place.
2. Greedy accumulation over the **sorted** order:
   the running total starts at 0; for each track its contribution
   (`_track_limit_value(track, prefs.limit_type)`) is computed; the track
   is taken when total + contribution does not exceed
   `prefs.limit_value`, and only taken tracks add their contribution to
   the total. Tracks that would overflow are skipped but the scan
   **continues** (a later, smaller track may still fit). No early break.

**Phase 3 — output:** `[t["track_id"] for t in selected if "track_id" in t]`
— ids appear in selection order, no deduplication, no validity
filtering (callers filter against the live id set). When
`check_limits` is false there is **no sorting** — output order equals
input order.

**Track contribution per limit type** (`_track_limit_value`):

| `limit_type` | value | unit |
|---|---|---|
| 0x01 `SPL_LIMIT_TYPE_MINUTES` | `length / 60_000` | minutes of audio (`length` is ms) |
| 0x02 `SPL_LIMIT_TYPE_MB` | `size / 1024²` | MiB |
| 0x03 `SPL_LIMIT_TYPE_SONGS` | `1.0` | count |
| 0x04 `SPL_LIMIT_TYPE_HOURS` | `length / 3_600_000` | hours |
| 0x05 `SPL_LIMIT_TYPE_GB` | `size / 1024³` | GiB |
| anything else | `1.0` | treated as songs |

### 5.9 Limit sort orders — `_sort_key(limit_sort)`

`base = limit_sort & 0x7FFFFFFF` (high bit `0x80000000` is the
reverse/“least/lowest” flag); returns `(key_func, reverse)` for
`sorted()`:

| base | constant | key | reverse |
|---|---|---|---|
| 0x02 | `SPL_LIMIT_SORT_RANDOM` | `None` → caller shuffles instead of sorting | `False` |
| 0x03 | `SPL_LIMIT_SORT_SONG_NAME` | `(t.get("Title","") or "").casefold()` | `False` (reverse bit **ignored** for string sorts) |
| 0x04 | `SPL_LIMIT_SORT_ALBUM` | `(t.get("Album","") or "").casefold()` | `False` |
| 0x05 | `SPL_LIMIT_SORT_ARTIST` | `(t.get("Artist","") or "").casefold()` | `False` |
| 0x07 | `SPL_LIMIT_SORT_GENRE` | `(t.get("Genre","") or "").casefold()` | `False` |
| 0x10 | `SPL_LIMIT_SORT_MOST_RECENTLY_ADDED` | `t.get("date_added", 0)` | `not reverse_bit` → base sort is **descending** (newest first); with 0x80000010 (`LEAST_RECENTLY_ADDED`) it is ascending |
| 0x14 | `SPL_LIMIT_SORT_MOST_OFTEN_PLAYED` | `t.get("play_count_1", 0)` | `not reverse_bit` (0x80000014 → ascending) |
| 0x15 | `SPL_LIMIT_SORT_MOST_RECENTLY_PLAYED` | `t.get("last_played", 0)` | `not reverse_bit` (0x80000015 → ascending) |
| 0x17 | `SPL_LIMIT_SORT_HIGHEST_RATING` | `t.get("rating", 0)` | `not reverse_bit` (0x80000017 → ascending) |
| anything else | — | constant `lambda t: 0` | `False` |

String sorts use `sorted(key=…, reverse=False)` regardless of the high
bit; numeric “most/highest” sorts put the highest value first.

### 5.10 `spl_update_from_parsed` and `spl_update_all`

```python
def spl_update_from_parsed(parsed_prefs: dict, parsed_rules: dict,
                           tracks: list[dict],
                           playlist_lookup=None, time_context=None) -> list[int]
def spl_update_all(playlists: list[dict], tracks: list[dict],
                   live_only: bool = False) -> dict[str, list[int]]
```

* `spl_update_from_parsed` converts the raw dicts through
  `prefs_from_parsed` / `rules_from_parsed` (chapter 04) and delegates
  to `spl_update`.
* `spl_update_all` operates on **raw parsed playlist rows**:
  1. Build `playlist_lookup`: for every row with a non-empty `items`
     list, `playlist_lookup[pl.get("playlist_id", 0)] = {item.get("track_id", 0)
     for item in items}` — raw parser track ids (this variant never
     remaps to db ids; its `tracks` carry parser `track_id`s too). Rows
     with no items get **no** entry (unlike the builder lookup, §6.6).
  2. Iterate rows; skip rows without truthy `smart_playlist_data`, rows
     where `smart_playlist_rules` is `None`, and — when `live_only` —
     rows whose `prefs_data.get("live_update", False)` is falsy.
  3. Key each result by `pl.get("Title", "?")` (later rows with the same
     title overwrite earlier ones) and value it with
     `spl_update_from_parsed(prefs, rules, tracks, lookup)` (no
     `time_context` → device/UTC default).

---

## 6. `podsync.sync._playlist_builder`

Builds `PlaylistInfo` lists for the three playlist datasets from parsed
rows and evaluates smart-playlist membership. Output feeds
`write_itunesdb` directly (chapter 04).

### 6.1 Playlist sort orders (firmware does not sort — we must)

`_SORT_ORDER_KEYS: dict[int, list[(dict_key, is_string, sort_override_key|None)]]`.
Sort is **stable**; string keys are `casefold()`ed, numeric keys compare
ascending; a `sort_override` (“Sort Title” etc.) is used only when it is
truthy, otherwise the base key. Values `0` (default) and `1` (manual),
and unknown values → **no sort, order preserved**.

| `sort_order` | key sequence (in priority order) |
|---|---|
| 0 / 1 / unknown | no sort |
| 3 | `Title` ▸ override `Sort Title` |
| 4 | `Album` ▸ `Sort Album`, then `disc_number`, then `track_number` |
| 5 | `Artist` ▸ `Sort Artist`, `Album` ▸ `Sort Album`, `disc_number`, `track_number` |
| 6 | `bitrate` |
| 7 | `Genre`, `Artist` ▸ `Sort Artist`, `Album` ▸ `Sort Album`, `track_number` |
| 8 | `filetype` |
| 9 | `last_modified` |
| 10 | `disc_number`, `track_number` |
| 11 | `size` |
| 12 | `length` |
| 13 | `year`, `Artist` ▸ `Sort Artist`, `Album` ▸ `Sort Album` |
| 14 | `sample_rate_1` |
| 15 | `Comment` |
| 16 | `date_added` |
| 17 | `eq_setting` |
| 18 | `Composer` |
| 20 | `play_count_1` |
| 21 | `last_played` |
| 22 | `disc_number`, `track_number` |
| 23 | `rating` |
| 24 | `date_released` |
| 25 | `bpm` |
| 26 | `Grouping` |

Key construction for **parsed dicts** (`_sort_key_for_track`): for each
triple, take the override when truthy else `track.get(field)`; `None` →
`""` (string) / `0` (numeric); strings → `str(val).casefold()`; numerics
→ the value only if `isinstance(val, (int, float))`, else `0`.

Key construction for **`TrackInfo`** (`_sort_key_for_trackinfo`): the
override is ignored; the dict key is translated through this attribute
table, then `getattr(ti, attr, None)` with the same None/fallback
rules:

| dict key | attribute | dict key | attribute |
|---|---|---|---|
| `Title` | `title` | `bitrate` | `bitrate` |
| `Artist` | `artist` | `size` | `size` |
| `Album` | `album` | `length` | `length` |
| `Album Artist` | `album_artist` | `year` | `year` |
| `Genre` | `genre` | `track_number` | `track_number` |
| `Composer` | `composer` | `disc_number` | `disc_number` |
| `Comment` | `comment` | `bpm` | `bpm` |
| `Grouping` | `grouping` | `rating` | `rating` |
| `filetype` | `filetype` | `play_count_1` | `play_count` |
| `sample_rate_1` | `sample_rate` | `skip_count` | `skip_count` |
| `date_added` | `date_added` | `last_played` | `last_played` |
| `last_modified` | `last_modified` | `date_released` | `date_released` |

Every dict key maps to the `TrackInfo` attribute of the same meaning, so
sort orders 9 (date modified) and 24 (release date) sort `TrackInfo` lists
by those timestamps just like parsed dicts.

Public helpers:

```python
def sort_tracks_by_order(tracks: list[dict], sort_order: int) -> list[dict]
def sort_trackinfos_by_order(track_ids: list[int], sort_order: int,
                             db_track_id_to_info: dict[int, TrackInfo]) -> list[int]
```

* `sort_tracks_by_order`: unknown/manual order → same list object;
  otherwise `sorted(tracks, key=…)`.
* `sort_trackinfos_by_order`: ids are partitioned into *known* (present
  in `db_track_id_to_info`) and *unknown*; known ids are sorted by the
  `TrackInfo` key; **unknown ids are appended at the end in their
  original order**. Unknown/manual order → input unchanged.

```python
def decode_raw_blob(value) -> bytes | None
```

`None` → `None`; `bytes` → as-is; `str` → `base64.b64decode(value)`
(any exception → `None` — the parser may have base64-ized raw MHOD bytes
for JSON round-tripping); any other type → `None`.

### 6.2 `build_and_evaluate_playlists` — signature and contract

```python
def build_and_evaluate_playlists(
    existing_tracks_data: list[dict],
    dataset2_standard_playlists_raw: list[dict],
    dataset3_podcast_playlists_raw: list[dict],
    dataset5_smart_playlists_raw: list[dict],
    all_track_infos: list[TrackInfo],
    source_path_to_db_track_id: dict[str, int] | None = None,
    time_context: DeviceTimeContext | None = None,
) -> tuple[str, int | None, list[PlaylistInfo],
           str, int | None, list[PlaylistInfo],
           list[PlaylistInfo]]
```

Return tuple, in order:

| # | element | meaning |
|---|---|---|
| 1 | `dataset2_master_name` | name of the dataset-2 master row, default `"iPod"` |
| 2 | `dataset2_master_id` | its `playlist_id`, `None` if no master row |
| 3 | `dataset2_playlists` | non-master `PlaylistInfo` rows (incl. folders) |
| 4 | `dataset3_master_name` | dataset-3 master name, default `"iPod"` |
| 5 | `dataset3_master_id` | its `playlist_id`, `None` if no master row |
| 6 | `dataset3_playlists` | non-master podcast-dataset rows |
| 7 | `dataset5_playlists` | smart/category rows |

All inputs are treated as read-only except the produced
`PlaylistInfo` objects.

### 6.3 Algorithm (exact order)

1. **Hierarchy reconciliation.** Run
   `reconcile_playlist_hierarchy(...)` (chapter 03) over the dataset-2
   and dataset-3 raw row lists (dataset 5 is *not* reconciled). This is
   pure: it returns copied rows in stable folder preorder, detaches
   dangling/cyclic folder parents, rebuilds each folder row's `items` as
   the aggregate of its children (invalid ids dropped, physical order
   preserved), and synthesizes folder `smart_playlist_data` /
   `smart_playlist_rules` (`check_rules` true, conjunction `"OR"`, one
   field-0x28 rule per child playlist) so folders flow through the same
   code paths as smart playlists.
2. **Old-id map.** `old_tid_to_db_track_id[track_id] = db_track_id` for
   every row of `existing_tracks_data` where both
   `coerce_int(track_id)` and `coerce_int(db_track_id or db_id)` are
   non-zero.
3. **Valid id set.** `valid_db_track_ids = {coerce_int(ti.db_track_id)
   for ti in all_track_infos}` minus zeros.
4. **Eval corpus.** `eval_tracks = [trackinfo_to_eval_dict(t) for t in
   all_track_infos]` (their `track_id` key is the db id).
5. **Source-path lookup.** Start with
   `stable_path_key(str(ti.source_path)) → coerce_int(ti.db_track_id)`
   for every `TrackInfo` that has a non-empty `source_path` and non-zero
   db id; then update with `source_path_to_db_track_id` (each raw key
   re-keyed through `stable_path_key`, values coerced). The update makes
   the caller-supplied mapping win on collisions.
6. **Initial playlist lookup** (`_playlist_lookup_from_rows`) over
   `[ds2 rows, ds3 rows, ds5 rows]` (§6.6) — membership keyed by
   `int(playlist_id)`, resolved to db ids, master rows skipped.
7. **Dataset 2** → `_build_standard_dataset_playlists(...,
   source_dataset_name="dataset2", ...)` (§6.4).
8. **Dataset 3** → same with `"dataset3"`.
9. **Dataset 5** → `_build_smart_playlists(...)` (§6.5).
10. **Live re-evaluation pass** (`_reevaluate_live_update`) over
    `ds2 + ds3` playlists plus dataset-5 rows with falsy `mhsd5_type`
    (§6.7).
11. **Podcast membership sync** (`_sync_podcast_playlist_membership`)
    (§6.8).
12. **Apply sort orders** to `playlists + podcast_playlists +
    smart_playlists` (§6.9).
13. Return the 7-tuple (§6.2).

### 6.4 Datasets 2 and 3 — `_build_standard_dataset_playlists`

Inputs: reconciled rows, the maps from §6.3, `eval_tracks`,
`spl_update`, `source_lookup`, source name (`"dataset2"`/`"dataset3"`),
initial lookup, `time_context`. Returns
`(master_name, master_id, rows)`.

**Master handling:**

* Defaults: `master_playlist_name = "iPod"`, `master_playlist_id = None`.
* Rows with truthy `master_flag` are *not* appended to the output; the
  **first** sets `master_playlist_name = pl.get("Title", "iPod")` and
  `master_playlist_id = pl.get("playlist_id")` (raw value, may be `None`
  or non-int).
* **More than one master row → `ValueError`** whose message is exactly
  `f"{source_dataset_name} contains {len(master_rows)} master_flag playlist rows"`
  — e.g. `"dataset2 contains 2 master_flag playlist rows"`. Malformed
  input is refused, never guessed into shape.

**Non-master row → `PlaylistInfo` field mapping:**

| `PlaylistInfo` field | source |
|---|---|
| `name` | `pl.get("Title", "Untitled")` |
| `track_ids`, `item_metadata` | §6.6 resolution |
| `playlist_id` | `pl.get("playlist_id")` (raw; `None` allowed → writer generates) |
| `master` | `False` |
| `sortorder` | `pl.get("sort_order", 0)` |
| `podcast_flag` | `pl.get("podcast_flag", 0)` |
| `playlist_kind_flags` | `pl.get("playlist_kind_flags")` (`None` allowed — `__post_init__` then normalizes from `podcast_flag`) |
| `parent_folder_playlist_id` | `coerce_int(pl.get("parent_folder_playlist_id", pl.get("unk0x30_playlist_ref", 0)))` (explicit parent wins over the legacy word; missing → 0; `None` → 0) |
| `mhsd5_type` | `coerce_int(pl.get("mhsd5_type", 0))` — a dataset-5 marker sitting in a dataset-2/3 row is **preserved verbatim, never repaired** |
| `phase_game_flag` | `coerce_int(pl.get("phase_game_flag", 0))` (raw MHYP +0x52 word, e.g. 25 for Phase Music playlists) |
| `raw_mhod100` | `decode_raw_blob(pl.get("playlist_prefs"))` |
| `raw_mhod102` | `decode_raw_blob(pl.get("playlist_settings"))` |
| `raw_mhod55` | `playlist_property_raw_body_for_write(pl)` |
| `playlist_description` | `_playlist_description(pl)` (§6.10): `d = playlist_description_from_row(pl)`; `d` when it is truthy **or** the row contains the key `"playlist_description"` (so an explicit empty description stays `""`), otherwise `None` |

**Smart-playlist handling inside a dataset-2/3 row:**

* Requires **both** `pl["smart_playlist_data"]` and
  `pl["smart_playlist_rules"]` truthy; then
  `info.smart_prefs = prefs_from_parsed(...)`,
  `info.smart_rules = rules_from_parsed(...)`.
* Re-evaluate (`spl_update(smart_prefs, smart_rules, eval_tracks,
  initial_lookup, time_context)`) iff
  `smart_prefs.live_update and not info.is_folder`; replace `track_ids`
  with `[d for d in matched if d in valid_db_track_ids]`, set
  `item_metadata = None`.
* Otherwise keep the parsed membership untouched (static/snapshot smart
  playlists with `live_update == false` keep their fixed membership
  while retaining rule data; **folders never** go through `spl_update`
  here — their reconciled aggregate membership is authoritative).
* Note that `mhsd5_type` does **not** gate evaluation in datasets 2/3 —
  a category-marker row in the visible bucket is still evaluated when
  live.
* A smart row without items and with empty rule lists evaluates to
  “match all checked tracks” (per §5.8 Phase 1 rule 3), then validity
  filtering applies.

### 6.5 Dataset 5 — `_build_smart_playlists`

For each row (no master-name extraction, no folder reconciliation):

* `PlaylistInfo(name=pl.get("Title","Untitled"), playlist_id=pl.get("playlist_id"),
  master=bool(pl.get("master_flag", 0)), track_ids, sortorder=pl.get("sort_order", 0),
  mhsd5_type=coerce_int(pl.get("mhsd5_type", 0)), phase_game_flag,
  raw_mhod100, raw_mhod102, raw_mhod55, playlist_description, item_metadata)`
  — `podcast_flag`/`playlist_kind_flags`/`parent_folder_playlist_id`
  are **not** passed (dataset-5 rows use `master_flag`/`mhsd5_type` only).
  `master` mirrors the parsed flag exactly (no inference from
  `mhsd5_type`).
* Rule conversion as in §6.4 (both dicts required).
* Evaluation gate: `not info.mhsd5_type and info.smart_prefs.live_update`
  → `spl_update` with the initial lookup, then validity filtering and
  `item_metadata = None`.
* **`mhsd5_type != 0` rows (categories: Movies=2, TV Shows=3, Music=4,
  Audiobooks=5, Ringtones=6, Rentals=7) keep their parsed membership and
  parsed item metadata** — they are firmware category containers, not
  user smart playlists.
* `mhsd5_type` accepts string values from caches (`"4"` → `4` via
  `coerce_int`).

### 6.6 Track membership and lookup construction

**Item resolution** — `_playlist_track_ids_and_metadata(items,
old_tid_to_db_track_id, valid_db_track_ids, source_lookup)` returns
`(list[int], list[PlaylistItemMeta] | None)`:

For each parsed MHIP row (dict), resolve a db id in this order:

1. `old_tid_to_db_track_id.get(coerce_int(item.get("track_id", 0)), 0)`
   — the row's parser track id mapped through the old-id map;
2. if that yields 0: `item.get("db_track_id", item.get("db_id", 0))` —
   rows cached with db ids directly;
3. if still 0: `item.get("source_path") or item.get("_source_path")`,
   looked up as `source_lookup.get(stable_path_key(str(path)), 0)`;
4. `coerce_int(...)`.

Keep the id **only if it is in `valid_db_track_ids`** (dangling
references to removed tracks are dropped). For each kept id append
`PlaylistItemMeta(podcast_group_flag=item.get("podcast_group_flag", 0),
group_id=item.get("group_id", 0),
podcast_group_ref=item.get("group_id_ref", 0),
track_persistent_id=item.get("track_persistent_id", 0),
mhip_persistent_id=item.get("mhip_persistent_id", 0))`. Return
`item_meta if item_meta else None` (i.e. `None` when nothing was kept).

**Playlist-membership lookups** used by field-0x28 rules:

* `_playlist_lookup_from_rows(rows, …)` — for every row that is **not** a
  master row and whose `playlist_id` is truthy and accepted by `int()`
  (`TypeError`/`ValueError` → row skipped):
  `lookup.setdefault(int(playlist_id), set()).update(resolved_ids)`.
  Entries are created **even for playlists with no items** (empty set →
  “is in playlist” matches nothing, “is not in playlist” matches
  everything). Master rows and rows with falsy/unparsable ids are
  skipped.
* `_playlist_lookup_from_infos(playlist_groups)` — same shape over built
  `PlaylistInfo` objects: skip `playlist_id is None` or `master`;
  `setdefault(int(id), set()).update(info.track_ids)`.

Both lookups map **playlist id → set of db track ids**, matching the
`track_id` key of eval dicts (§4.4). Playlist ids are full 64-bit ints
(rule `from_value` equals them exactly).

### 6.7 Live re-evaluation — `_reevaluate_live_update`

Scope: every playlist in `ds2 + ds3`, plus dataset-5 playlists whose
`mhsd5_type` is falsy (category containers excluded). A playlist is
re-evaluated iff `smart_prefs and smart_rules and
smart_prefs.live_update and not info.is_folder`.

For each qualifying playlist, **rebuild the lookup from the current
`PlaylistInfo` objects** (`_playlist_lookup_from_infos([playlists,
smart_playlists])`) so “is in playlist” rules see up-to-date membership
(including other smart playlists), then run `spl_update` against the
final `eval_tracks` and `time_context`, filter to
`valid_db_track_ids`, and if the resulting id list differs from the
current one, assign it and clear `item_metadata = None` (positional
MHODs would otherwise be stale). Folder aggregates and static
(`live_update == false`) playlists are never re-evaluated here.

### 6.8 Podcast membership — `_sync_podcast_playlist_membership`

* `podcast_db_track_ids = [t.db_track_id for t in all_track_infos if
  t.db_track_id and (t.media_type & MEDIA_TYPE_PODCAST or t.podcast_flag)]`
  — order follows `all_track_infos`.
* Targets: every playlist in `[*playlists, *podcast_playlists]` with
  `playlist.is_podcast` true (kind-flag podcast bit; a folder with
  `playlist_kind_flags == 0x0100` is **not** a podcast target).
* If there are **no** targets but podcast tracks exist: append
  `PlaylistInfo(name="Podcasts", track_ids=[], podcast_flag=1)` to
  `podcast_playlists` (dataset 3) and treat it as a target.
* For every target: if `track_ids != podcast_db_track_ids` → replace
  with `list(podcast_db_track_ids)` and clear `item_metadata = None`.
  The special Podcasts playlist therefore always mirrors the podcast
  tracks exactly, in **both** dataset 2 and dataset 3 mirrors when both
  contain one.

### 6.9 Final sort application

For each playlist in `playlists + podcast_playlists + smart_playlists`:

* If `pl.sortorder not in (0, 1)` **and** `pl.track_ids` is non-empty:
  * `pl.track_ids = sort_trackinfos_by_order(pl.track_ids, pl.sortorder,
    db_track_id_to_info)` where `db_track_id_to_info = {t.db_track_id: t
    for t in all_track_infos if t.db_track_id}`;
  * `pl.item_metadata = None` — **always** in this branch, even when the
    sort order was unknown and `sort_trackinfos_by_order` returned the
    list unchanged (positional item metadata is dropped anyway).
* Unknown `db_track_id`s in the list survive at the tail (§6.1).

### 6.10 Module-level helpers (also public)

| function | behavior |
|---|---|
| `sort_tracks_by_order` / `sort_trackinfos_by_order` / `decode_raw_blob` | §6.1 |
| `_mhsd5_type_value(pl)` | `coerce_int(pl.get("mhsd5_type", 0))` |
| `_phase_game_flag_value(pl)` | `coerce_int(pl.get("phase_game_flag", 0))` |
| `_playlist_description(pl)` | `d = playlist_description_from_row(pl)`; return `d` if (`d` truthy or `"playlist_description" in pl`) else `None` |
| `_playlist_property_plist_raw(pl)` | delegates to `playlist_property_raw_body_for_write(pl)` |
| `_source_path_key(path)` | `stable_path_key(str)` |

---

## 7. `podsync.sync._db_io`

### 7.1 Exception

```python
class DatabaseVerificationError(RuntimeError): ...
```

Raised when a freshly written database fails read-back checks.

### 7.2 `read_existing_database`

```python
def read_existing_database(ipod_path: Path, *, raise_on_error: bool = False,
                           include_playcounts: bool = True) -> dict
```

**Side effects: reads only** — never writes. Files touched, in order:

| step | file(s) | via |
|---|---|---|
| 1 | locate DB: `podsync.device.resolve_itdb_path(str(ipod_path))`; if falsy → `<ipod_path>/iPod_Control/iTunes/iTunesDB` | chapter 06 |
| 2 | first `0x70` bytes of the DB (magic `b"mhbd"`, int32-LE UTC offset at `0x6C`) | `struct` |
| 3 | device time context: `read_device_time_context(ipod_path, database_offset=…)` (+ whatever device preference files it reads, chapter 07) and `timezone_changed_since_database(...)` | `podsync.itunesdb_shared.device_time` |
| 4 | full DB parse: `parse_itunesdb(str(itdb_path), time_context=database_time_context)` | chapter 02 |
| 5 | artwork refs: `hydrate_track_artwork_refs(tracks, itdb_path)` | chapter 02 |
| 6 | `iPod_Control/iTunes/Play Counts` (only when `include_playcounts`) | chapter 02 |
| 7 | `iPod_Control/iTunes/OTGPlaylistInfo*` (via `load_otg_playlists(str(itdb_path.parent), tracks)`) | chapter 02 |

**Algorithm:**

1. **Missing DB:** if the located path does not exist —
   `raise_on_error=True` → `FileNotFoundError(f"iTunesDB was not found at {itdb_path}")`;
   else return the *empty result* (below).
2. Read the header (step 2). `database_offset` is set only when
   `len(header) >= 0x70 and header[:4] == b"mhbd"`, else `None`.
3. Build the time context (step 3). If the timezone changed since the
   database was written **and** `database_offset is not None`, parse with
   `database_time_context = DeviceTimeContext.fixed_offset(database_offset)`;
   otherwise parse with `device_time_context`.
4. Parse (step 4) and `data = extract_datasets(raw)`; tracks are
   `data.get("mhlt", [])`.
5. **Flatten tracks:** for each track dict — `children = t.pop("children", [])`;
   `t.update(extract_mhod_strings(children))` (MHOD strings: Title,
   Artist, …); `t.update(extract_track_extras(children))` (non-string
   extras); if `"filetype"` present →
   `t["filetype"] = filetype_to_string(t["filetype"])` (numeric code →
   string such as `"MP3"`).
6. **Artwork hydration hook:** `hydrate_track_artwork_refs(tracks,
   itdb_path)` — resolves parsed artwork references against ArtworkDB
   after flattening and before play-count merging (chapter 02 owns the
   function; the call must remain unconditional).
7. **Play Counts merge** (§7.2.6).
8. **Playlist datasets** — extract the three lists *separately*:
   `data.get("mhlp", [])` → dataset 2, `data.get("mhlp_podcast", [])`
   → dataset 3, `data.get("mhlp_smart", [])` → dataset 5. Apply
   `_process_playlist_list` to each:
   * `mhod_children = pl.pop("mhod_children", [])`;
     `pl.update(extract_mhod_strings(mhod_children))` (Title, …);
     `pl.update(extract_playlist_extras(mhod_children))` (sort order,
     flags, `smart_playlist_data`, `smart_playlist_rules`, …);
   * `mhip_children = pl.pop("mhip_children", [])`; each child is
     wrapped as `{"chunk_type": …, "data": {...}}`; children without
     `"data"` are skipped; otherwise take the inner dict, `item.update(
     extract_playlist_item_extras(item.get("children", [])))`, append;
     finally `pl["items"] = items` (always set, possibly `[]`).
9. **OTG merge:** collect `dataset2_seen_ids =
   {int(pl["playlist_id"]) for pl in ds2 if pl.get("playlist_id")}`;
   `otg = load_otg_playlists(str(itdb_path.parent), tracks)`; append each
   OTG row to **dataset 2 only** whose `int(pl.get("playlist_id") or 0)`
   is non-zero and not already seen (add to the set on append). Never
   inject into dataset 3 or 5.
10. Log counts (`Parsed iPod database: %d tracks, ds2_playlists=…`),
    return the *full result*.

**Result shapes (exact):**

```python
# success (note: 6 keys)
{
  "tracks": [...],                        # flattened, extras applied, artwork hydrated
  "dataset2_standard_playlists": [...],   # incl. OTG rows merged in
  "dataset3_podcast_playlists": [...],
  "dataset5_smart_playlists": [...],
  "playcounts_timezone_changed": bool,    # timezone_changed
  "device_time_context": DeviceTimeContext,   # the CURRENT device zone
}
# empty result (missing DB or caught error with raise_on_error=False) — 5 keys ONLY
{
  "tracks": [],
  "dataset2_standard_playlists": [],
  "dataset3_podcast_playlists": [],
  "dataset5_smart_playlists": [],
  "playcounts_timezone_changed": False,
  # NO "device_time_context" key — do not add it
}
```

There is deliberately **no** `"playlists"` and no `"smart_playlists"`
key: dataset lists stay strictly separate (dataset 2 and 3 are both
MHLP lists with different firmware semantics).

**Exceptions:** missing file with `raise_on_error` → `FileNotFoundError`
(above, raised *outside* the general handler). Any other exception inside
the parse → `logger.error("Failed to parse iTunesDB: %s", e)`, then
re-raise when `raise_on_error=True`, else return the empty result.

**7.2.6 Play Counts merge semantics** (after step 6):

* `pc_path = ipod_path / "iPod_Control" / "iTunes" / "Play Counts"`;
  `entries = parse_playcounts(pc_path)` (chapter 02; `None` = no file).
* If `entries is not None`:
  * if `timezone_changed` and any `entry.has_data` → log a warning that
    the merge uses the current device zone and plays before the change
    may be offset (message includes `database_offset` and
    `device_time_context.name`);
  * `merge_playcounts(tracks, entries, time_context=device_time_context)`
    — **in place**, over `min(len(tracks), len(entries))` rows. After
    merging: `play_count_1` = new cumulative plays; `skip_count` = new
    cumulative skips; `play_count_2` = durable queue of plays not yet
    scrobbled (independent of the current file delta); `recent_playcount`
    / `recent_skipcount` = this session's deltas; `rating`,
    `last_played`, `last_skipped` may be overridden by on-device user
    action (`rating >= 0` overrides, `-1` means “no change”). No
    additional defaulting is done in this branch.
* If `entries is None`, **or** `include_playcounts=False`: every track
  gets `t.setdefault("recent_playcount", 0)` and
  `t.setdefault("recent_skipcount", 0)`.

### 7.3 `write_database`

```python
def write_database(
    ipod_path: Path,
    tracks: list[TrackInfo],
    pc_file_paths: dict | None = None,
    playlists: list[PlaylistInfo] | None = None,
    podcast_playlists: list[PlaylistInfo] | None = None,
    smart_playlists: list[PlaylistInfo] | None = None,
    master_playlist_name: str = "iPod",
    master_playlist_id: int | None = None,
    podcast_master_playlist_name: str | None = None,
    podcast_master_playlist_id: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
    raise_on_error: bool = False,
    case_sensitive_paths: bool | None = None,
    before_database_replace: Callable[[], None] | None = None,
    before_device_mutation: Callable[[], None] | None = None,
) -> bool
```

**7.3.1 Capability snapshot (before anything else).** In a
`try/except` (any failure → log debug, `capabilities = None`):
`dev = podsync.device.get_current_device_for_path(str(ipod_path))`;
`capabilities = dev.capabilities if dev and dev.model_family else None`.
The **selected device's resolved capability snapshot** is passed
through unchanged — reconstructing capabilities from family/generation
alone would lose capacity/model-number distinctions (e.g. the 64 MiB
database limit of 80 GB 5.5G units) and trusted SysInfo overrides. A
test that stubs `get_current_device_for_path` to return an object with
`model_family` and a `capabilities` instance must observe
`captured_kwargs["capabilities"] is capabilities`.

**7.3.2 Serialize.** Call
`podsync.itunesdb_writer.write_itunesdb` (chapter 04) resolved through
the package at call time, with **exactly** these arguments:

```python
write_itunesdb(str(ipod_path), tracks,
    pc_file_paths=pc_file_paths, playlists=playlists,
    podcast_playlists=podcast_playlists, smart_playlists=smart_playlists,
    capabilities=capabilities,
    master_playlist_name=master_playlist_name, master_playlist_id=master_playlist_id,
    podcast_master_playlist_name=podcast_master_playlist_name,
    podcast_master_playlist_id=podcast_master_playlist_id,
    progress_callback=progress_callback,
    before_database_replace=before_database_replace,
    before_device_mutation=before_device_mutation)
```

`pc_file_paths` (db_track_id → PC artwork source path) drives ArtworkDB
+ `.ithmb` writing; `None`/empty → no artwork pass. The two hook
callbacks are forwarded verbatim — the writer invokes
`before_database_replace` immediately before replacing the DB file and
`before_device_mutation` before each device mutation (chapter 04/07 owns
their exact call points). `db_id`, `backup`, `force_checksum`,
`firewire_id`, `reference_itdb_path` are **not** passed (writer
defaults).

* If `write_itunesdb` **raises**: log the exception
  (`Database write failed during iTunesDB serialization; output was not
  committed. …`); `raise_on_error=True` → propagate the *original*
  exception unchanged; else return `False`.
* If it returns falsy → return `False` (no verification).

**7.3.3 SQLite routing (nano 5G-7G).** Before calling `write_itunesdb`, evaluate:

```
sqlite_required = (capabilities is not None and capabilities.uses_sqlite_db)
                  or os.path.isdir(<ipod_path>/iPod_Control/iTunes/iTunes Library.itlp)
```

If `sqlite_required`, `write_database` calls
`podsync.itdb.sqlite.write_sqlite_databases` instead of `write_itunesdb`
(chapter 04 §6.5) — resolving `checksum_kind` from `capabilities.checksum`
and, for HASH58/HASHAB, a `firewire_id` via `hardware.get_firewire_id`
(a missing FireWire ID here follows the same log-and-return-`False`/
`raise_on_error` rule as any other write failure, not a static refusal).
Since there is no SQLite reader, this path returns right after the write —
§7.3.4's read-back verification only applies to the classic path.

**7.3.4 Read-back verification (after a successful serialization).**
Call the module-global `verify_written_database(ipod_path,
expected_track_count=len(tracks), case_sensitive_paths=case_sensitive_paths)`
(§7.4). A `DatabaseVerificationError` is logged; `raise_on_error=True`
→ re-raise; else return `False`.

**7.3.5 Return.** `True` only when the writer reported success **and**
verification passed. Summary of outcomes:

| condition | result |
|---|---|
| SQLite-required device | routes to `write_sqlite_databases` (§7.3.3); no read-back verification |
| writer raised, `raise_on_error=False` | `False` |
| writer raised, `raise_on_error=True` | original exception |
| writer returned falsy | `False` |
| verification failed, `raise_on_error=False` | `False` |
| verification failed, `raise_on_error=True` | `DatabaseVerificationError` |
| both phases OK | `True` |

**Safety ordering.** The protocol is: *(pre-write checks live outside
this module)* → serialize to a temp file + atomic replace inside
`write_itunesdb` (hooks fire there) → **reparse the committed database
and verify every media reference before reporting success**. Nothing is
reported as written until the read-back passes. `write_database` itself
does **not** delete media files, does **not** touch Play Counts/OTG
state files, and performs **no “is iTunes running” check** — process
detection does not exist anywhere in `podsync.sync`; pre-write refusal
(readiness inspection, volume lock) is the caller's responsibility via
`podsync.device` (chapter 07) and the writer's guard hooks.

### 7.4 `verify_written_database`

```python
def verify_written_database(ipod_path: Path, *, expected_track_count: int,
                            case_sensitive_paths: bool | None = None) -> None
```

Read-back protocol (raises `DatabaseVerificationError` on any problem,
returns `None` on success):

1. `parsed = read_existing_database(ipod_path, raise_on_error=True)`
   — **module-global** call (patchable). Any exception is wrapped:
   `DatabaseVerificationError(f"Freshly written iTunesDB could not be reparsed: {exc}")`
   with `from exc` (a missing/unparsable DB on an empty directory raises
   this, matching `"could not be reparsed"`).
2. `parsed_tracks = parsed.get("tracks", [])`.
3. **Count check:** `len(parsed_tracks) != expected_track_count` →
   problem `"track count mismatch (expected {n}, read back {m})"`.
4. **Case detection:** if `case_sensitive_paths is None` →
   `case_sensitive_paths = detect_filesystem_type(ipod_path) == "hfsx"`
   (`podsync.device.filesystem`, chapter 07). `ipod_root =
   ipod_path.resolve()`.
5. For each parsed track (order preserved), collect problems:
   * `title = str(track.get("Title") or "?")`;
   * `location = str(track.get("Location") or "").strip()`; empty →
     `"track '{title}' has no Location"` (continue);
   * `media_path = expected_ipod_track_file_path(ipod_path, location)`;
     `None` → `"track '{title}' has an invalid or outside the iPod media path {location}"`
     (continue) — covers grammar failures, traversal, symlink escapes,
     external paths;
   * `resolved = media_path.resolve()`; not relative to `ipod_root` →
     `"track '{title}' references media outside the iPod {location}"`
     (continue);
   * **duplicate check:** key =
     `_database_media_path_key(resolved, case_sensitive=case_sensitive_paths)`;
     if the key was seen → `"duplicate media location {location} (already referenced as {previous_location})"`,
     else record it (the first occurrence is stored, so a third duplicate
     reports the *first* location);
   * **existence:** `not resolved.is_file()` →
     `"track '{title}' references missing media {location}"` (checked
     even for duplicates).
6. If any problems: `detail = "; ".join(problems[:5])`, plus
   `f"; and {len(problems) - 5} more problem(s)"` when more than 5; raise
   `DatabaseVerificationError(f"Freshly written iTunesDB failed verification: {detail}")`.
7. Otherwise log
   `"Verified freshly written iTunesDB: %d tracks and all media paths exist"`.

Consequences (these are the behaviors smoke tests rely on): writing a
track whose media file is missing on the device → `write_database`
returns `False`; two tracks with the same `Location` (on a
case-insensitive filesystem) → `False`; a track referencing a file
outside the iPod → verification error containing `outside the iPod`.

**`_database_media_path_key(path, *, case_sensitive) -> str`** (private
but test-visible): `key = str(path).replace("\\", "/")`; return `key`
when `case_sensitive` else `key.casefold()`. So on HFSX (case-sensitive)
`/…/Song.mp3` and `/…/song.mp3` are distinct keys (duplicates allowed);
on FAT/HFS+ they collapse (duplicates rejected).

### 7.5 `delete_playcounts_files`

```python
def delete_playcounts_files(ipod_path: Path, *,
    before_device_mutation: Callable[[], None] | None = None) -> None
```

Deletes the device-generated sync-state files **after** a successful
database commit. Fixed list, in this exact order, relative to
`iPod_Control/iTunes`:

1. `Play Counts`
2. `iTunesStats`
3. `PlayCounts.plist`
4. `OTGPlaylistInfo`

Per file: (a) call `before_device_mutation()` first if provided —
**once per file, before path resolution** (4 hook calls when all four
files exist); (b) `path = resolve_device_path(ipod_path,
iPod_Control/iTunes/<name>, allowed_subtree=iPod_Control/iTunes)`;
(c) `durable_unlink(path, missing_ok=True)` — missing files are
fine. On success log
`"Cleared device-generated sync state {path}"`.

**Only these four exact names are removed.** Firmware-owned siblings
such as `OTGPlaylistInfo_1` are **not** touched.

**Failure:** `OSError` or `UnsafeDevicePathError` from hook resolution,
`resolve_device_path`, or the unlink → raise
`DeviceWriteSafetyError` (from `podsync.device.write_guard`, chapter 07):

```
"The iPod database was committed, but its device-generated sync state "
"could not be cleared ({name}): {exc}"
```

(with `from exc`) — the failing file's name is in parentheses, e.g.
`… could not be cleared (Play Counts): device rejected cleanup`; the
file remains on disk; earlier files in the list may already be gone.
Exceptions raised *by the hook itself* that are not `OSError`/
`UnsafeDevicePathError` (e.g. a `DeviceWriteSafetyError` from
revalidation) propagate unchanged.

### 7.6 `commit_playcounts_if_needed`

```python
def commit_playcounts_if_needed(ipod_path: Path) -> bool
```

Merges the firmware's `Play Counts` into the database immediately,
instead of waiting for the next full sync to pick it up during a read.
`False`, with the device untouched, when: `podsync.itdb.reader.play_stats
.read_play_stats` returns `None`/empty for `iTunes/Play Counts`, or every
entry's `has_data` is `False` (nothing played or skipped, no on-device
rating change); or (inside the guarded commit) the reloaded database has
no tracks to rebuild.

Otherwise: acquire a `WriteLock` (chapter 07) keyed by
`lock_key_for(check_write_ready(ipod_path))`, then, holding it,
`_commit_playcounts_guarded`:

1. `read_existing_database`-equivalent (`load_device_library`) — this is
   where the Play Counts deltas actually get folded into the track rows
   (§7.2.6); nothing here re-parses them a second time.
2. Convert every row to a `TrackRecord` and rebuild the full playlist set
   (`assemble_playlists`, chapter 03/04) from the reloaded rows, exactly
   as a caller doing a full sync commit would.
3. `write_database`-equivalent (`save_device_library`), with
   `before_database_replace=guard.assert_database_unchanged` and
   `before_device_mutation` revalidating write-readiness — same hooks
   §7.3 describes.
4. On success, `delete_playcounts_files`-equivalent
   (`clear_device_play_state`), revalidating before each file.

Returns `True` only once all of the above succeeded; a failed write
follows `save_device_library`'s own success/failure and exception rules
(§7.3) and this function returns `False` without attempting cleanup.

### 7.7 Side-effect ledger

| order | operation | files | mode |
|---|---|---|---|
| R1 | `read_existing_database` step 2–4 | `iTunes/iTunesDB` (or resolved `iTunesCDB`) | read header, then full parse |
| R2 | step 3 | device time/preferences files (chapter 07) | read |
| R3 | step 6 | `iTunes/Play Counts` | read (optional) |
| R4 | step 9 | `iTunes/OTGPlaylistInfo*` | read |
| W1 | `write_database` → `write_itunesdb` | `iTunes/iTunesDB` (+ `iTunes/iTunesDB` backup, `Artwork/*` when `pc_file_paths`) | write: temp file → verify → atomic replace; hooks `before_database_replace` / `before_device_mutation` fire here |
| W2 | `write_database` → `verify_written_database` | `iTunes/iTunesDB` | full reparse + stat of every media path (read-only) |
| D1 | `delete_playcounts_files` | `iTunes/Play Counts`, `iTunes/iTunesStats`, `iTunes/PlayCounts.plist`, `iTunes/OTGPlaylistInfo` | delete, in that order, hook before each |

Media files under `iPod_Control/Music` are **never created or deleted
by this package** — only referenced and checked.

### 7.8 Expected differences after a read → write cycle

Measured on an iTunes-written database from an iPod Video 5.5G (1 316
tracks): a full `read_existing_database` → `track_dict_to_info` →
`build_and_evaluate_playlists` → write keeps the MHBD header fields, the
dataset order, every `db_track_id`, all strings and the gapless data. The
following differences are **expected** and are not bugs:

| Field | Change | Reason |
|---|---|---|
| `play_count_1`, `play_count_2`, `skip_count`, `last_played`, `last_skipped`, `rating` | updated | Play Counts merge (§7.2.6) |
| MHIT `unk0xC4`, `genius_category_id`, `unk0x20C` | → 0 | not carried by `TrackInfo` (opaque; playback unaffected) |
| MHIT `audio_format_flag` | recomputed from the filetype (chapter 04 §4.2) | iTunes sometimes stores 0 for MP3 |
| MHIT `has_artwork` | 0 → 2 for tracks without art | writer rule (chapter 04 §4.2) |
| MHIT `db_track_id_2` | 0 → `db_track_id` | writer always mirrors it |
| MHIT `last_played` of `1970-01-01` in device-local time | → 0 | converts to a non-positive Unix time, which the writer stores as 0 |
| tracks without an MHOD 6 | stay without it | `filetype_desc` is `None` (§4.3) |
| MHIP `timestamp` | → 0 | writer rule (chapter 04 §4.8) |
| dataset-5 rows | gain `db_id_2` / `playlist_id_2` | writer rule for non-master rows (chapter 04 §4.11) |
| master playlist MHOD 52/53 set | regenerated | writer's fixed category list (chapter 04 §4.4) |

---

## 8. Cross-chapter call map

Exact external entry points used by `podsync.sync`, and where they are
specified:

| Caller (this chapter) | Step | Callee | Specified in |
|---|---|---|---|
| `_db_io.read_existing_database` | locate DB | `podsync.device.resolve_itdb_path` | ch. 06 |
| `_db_io.read_existing_database` | time zone | `podsync.itunesdb_shared.device_time.{read_device_time_context, timezone_changed_since_database, DeviceTimeContext}` | ch. 03 |
| `_db_io.read_existing_database` | parse | `podsync.itunesdb_parser.parse_itunesdb` | ch. 02 |
| `_db_io.read_existing_database` | flatten | `podsync.itunesdb_shared.extraction.{extract_datasets, extract_mhod_strings, extract_track_extras, extract_playlist_extras, extract_playlist_item_extras}`, `field_base.filetype_to_string` | ch. 03 |
| `_db_io.read_existing_database` | artwork hook | `podsync.itunesdb_parser.artwork_links.hydrate_track_artwork_refs` | ch. 02 |
| `_db_io.read_existing_database` | play counts | `podsync.itunesdb_parser.playcounts.{parse_playcounts, merge_playcounts}` | ch. 02 |
| `_db_io.read_existing_database` | OTG | `podsync.itunesdb_parser.otg.load_otg_playlists` | ch. 02 |
| `_db_io.write_database` | capabilities | `podsync.device.get_current_device_for_path` | ch. 06 |
| `_db_io.write_database` | serialize | `podsync.itunesdb_writer.write_itunesdb` | ch. 04 |
| `_db_io.verify_written_database` | fs case | `podsync.device.filesystem.detect_filesystem_type` | ch. 07 |
| `_db_io.verify_written_database` | media path | `podsync.sync.ipod_track_paths.expected_ipod_track_file_path` | §3 (here) |
| `_db_io.delete_playcounts_files` | safety/unlink | `podsync.device.path_safety.{resolve_device_path, UnsafeDevicePathError}`, `podsync.device.durability.durable_unlink`, `podsync.device.write_guard.DeviceWriteSafetyError` | ch. 07 |
| `_playlist_builder` | hierarchy | `podsync.itunesdb_shared.playlist_hierarchy.reconcile_playlist_hierarchy` | ch. 03 |
| `_playlist_builder` | properties | `podsync.itunesdb_shared.playlist_properties.{playlist_description_from_row, playlist_property_raw_body_for_write}` | ch. 03 |
| `_playlist_builder` | SPL dataclasses | `podsync.itunesdb_writer.mhod_spl_writer.{prefs_from_parsed, rules_from_parsed, SmartPlaylistRule, SmartPlaylistRules, SmartPlaylistPrefs, RuleGroup}` | ch. 04 |
| `_playlist_builder` | records | `podsync.itunesdb_writer.mhyp_writer.{PlaylistInfo, PlaylistItemMeta}`, `mhit_writer.TrackInfo` | ch. 04 |
| `_playlist_builder` | engine | `podsync.sync.spl_evaluator.spl_update`, `podsync.sync._track_conversion.trackinfo_to_eval_dict` | §4/§5 (here) |
| `spl_evaluator` | constants | `podsync.itunesdb_shared.mhod_defs.{SPL_FIELD_TYPE_MAP, SPL_HOST_*_FIELD_KEYS, SPL_LIMIT_*}` , `field_base.MAC_EPOCH_OFFSET`, `device_time.{DeviceTimeContext, current_device_time_context}` | ch. 03 |
| `ipod_track_paths` | path safety | `podsync.device.path_safety.{resolve_device_path, UnsafeDevicePathError}` | ch. 07 |

Not used anywhere in `podsync.sync`: `podsync.device.get_firewire_id`,
`podsync.device.write_readiness.*`,
`podsync.device.write_guard.DeviceWriteGuard`,
`podsync.device.filesystem_profile.FilesystemProfile`, and every module
listed in §1.3.

---

## 9. Exclusions recap (normative)

1. **The SQLite path (`podsync.itdb.sqlite`) is implemented** (§7.3.3,
   chapter 04 §6.5) — `write_database` routes SQLite-required devices there
   instead of refusing them. It has no reader; nothing in podsync can read
   an `iTunes Library.itlp` back.
2. **No `pc_track_to_info`** in `podsync.sync._track_conversion`; no
   `podsync.sync.pc_library`, `podsync.sync.transcoder`.
3. **`commit_playcounts_if_needed` / `_commit_playcounts_guarded`** live
   in `podsync.library.database` (§7.6) — a standalone commit outside a
   full sync still merges during a read (§7.2.6), rebuilds, writes and
   clears state on its own. No separate `podsync.sync.database_commit`
   module exists; the helper is small enough to sit directly in
   `database.py` alongside `load_device_library`/`save_device_library`.
4. **No `podsync.sync.quick_writes` / `mapping` / `contracts`**, no
   `gui` / `application` / `podcasts` imports; referencing them raises a
   clear import error.
5. **`podsync.sync.__init__` re-exports nothing** — import submodules
   directly.
