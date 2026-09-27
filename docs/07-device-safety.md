# Chapter 07 — Device Safety: Write Guards, Path/Storage Safety, Filesystem Profiling, Durability, Eject

This chapter specifies the twelve `podsync.device` modules that together form the
write-safety layer of the iPod database engine:

| # | Module (`podsync.device.<mod>`) | Responsibility |
|---|---|---|
| 1 | `write_guard` | Host-wide exclusive writer lock, DB generation snapshots, external-change detection |
| 2 | `write_readiness` | Single fail-closed gate that answers "may I write to this volume now?" |
| 3 | `path_safety` | Containment of untrusted device/host paths (`resolve_device_path`, `resolve_host_path`) |
| 4 | `storage_safety` | File-size limits, allocation rounding, free-space primitives |
| 5 | `filesystem` | Mounted-filesystem type detection and iTunesDB platform-flag resolution |
| 6 | `filesystem_profile` | Per-OS volume fact collection, `FilesystemProfile`, revalidation |
| 7 | `durability` | Temp-file creation, fsync barriers, atomic replace/publish/unlink, volume flush |
| 8 | `eject` | Safe per-OS eject flow with flush-first and never-forced semantics |
| 9 | `metadata_write` | Contained, identity-locked atomic writes of small metadata files (SysInfo etc.) |
| 10 | `recovery` | Read-only Linux mount facts and non-writing recovery command plans |
| 11 | `dump` | Read-only JSON diagnostic dump (`python -m podsync.device.dump`) |
| 12 | `linux_integration` | Least-privilege Linux udev serial integration state + setup script text |

**Naming convention used throughout this chapter:** every user-facing message that
names the application uses the literal string `podsync`; the device itself
is always called `iPod` ("the iPod database", "this iPod"). Smoke tests only match
brand-free substrings (e.g. `"already writing"`, `"different volume"`,
`"not mounted"`), so the exact prefix words before those substrings are
implementation detail as long as the substrings appear verbatim. Filesystem
artifacts created by these modules use these exact names:

| Artifact | Exact name |
|---|---|
| Host lock directory | `{tempdir}/podsync-device-locks` (POSIX: `podsync-device-locks-{os.getuid()}`) |
| Case-sensitivity probe file prefix | `.Podsync_CaseProbe_Aa_` |
| Sibling temp file prefix / suffix | `.iop-` / `.tmp` |
| udev rule filename / destination | `61-podsync.rules` / `/etc/udev/rules.d/61-podsync.rules` |
| udev rule resource | `podsync/assets/linux/61-podsync.rules` (via `importlib.resources`) |
| udev published properties | `ID_PODSYNC_PRODUCT_SERIAL`, `ID_PODSYNC_RULE_VERSION` |
| Setup script heredoc delimiter | `PODSYNC_UDEV_RULE`; log prefix `podsync: `; failure prefix `podsync Linux identity setup failed: ` |

---

## 1. Error hierarchy and cross-module conventions

```
RuntimeError
└── DeviceWriteSafetyError            (podsync.device.write_guard)
    ├── DeviceBusyError               (podsync.device.write_guard)
    ├── ExternalDatabaseChangeError   (podsync.device.write_guard)
    └── FileSizeLimitError            (podsync.device.storage_safety)

ValueError
├── UnsafeDevicePathError             (podsync.device.path_safety)
└── UnsafeHostPathError               (podsync.device.path_safety)
```

Rules that hold for the whole chapter:

* **Fail closed.** Any fact that cannot be established (identity incomplete,
  filesystem type unknown, mount table unreadable, command unavailable when the
  barrier is mandatory) is an error or an explicit `False` result — never a
  silent success.
* **Platform dispatch** uses `sys.platform`: `win32`, `darwin`,
  `linux*` (via `sys.platform.startswith("linux")`). Any other platform gets
  "unavailable" facts and unsupported-operation results, never a crash.
* **Interop with Chapter 06 modules** (referenced by name only, not re-specified
  here): `podsync.device.info.resolve_itdb_path`,
  `podsync.device.info.itdb_write_filename`, `podsync.device.info.get_current_device`,
  `podsync.device.virtual_identity.virtual_ipod_profile`, the `podsync.device.scanner`
  private probes used by `dump`, and `podsync.device.sysinfo` parsers used by `dump`.
* **Logging:** the guard, readiness checks, and successful paths are silent at
  `INFO`. Diagnostics use `DEBUG`; refusals that a caller turns into an exception
  also log `WARNING` (see per-module rules). Smoke tests assert
  `caplog.records == []` at `INFO`/`DEBUG` for clean success paths.

---

## 2. `podsync.device.write_guard`

### 2.1 Public API

```python
class DeviceWriteSafetyError(RuntimeError): ...
class DeviceBusyError(DeviceWriteSafetyError): ...
class ExternalDatabaseChangeError(DeviceWriteSafetyError): ...

@dataclass(frozen=True, slots=True)
class DatabaseGeneration:
    filename: str
    exists: bool
    size: int = 0
    modified_ns: int = 0
    device: int = 0
    inode: int = 0
    digest: str = ""

def capture_database_generation(ipod_path: str | Path) -> DatabaseGeneration: ...

class DeviceWriteGuard:
    def __init__(
        self,
        ipod_path: str | Path,
        *,
        volume_key: str = "",
        expected_database_generation: DatabaseGeneration | None = None,
        track_database_generation: bool = True,
        lock_dir: str | Path | None = None,
        queue_in_process: bool = True,
    ) -> None: ...
    def __enter__(self) -> DeviceWriteGuard: ...
    def __exit__(self, _exc_type, _exc, _traceback) -> None: ...
    @property
    def starting_database_generation(self) -> DatabaseGeneration: ...
    def assert_database_unchanged(self) -> None: ...
    def refresh_database_generation(self) -> None: ...
    # public attribute set in __init__:
    lock_path: Path          # base_dir / f"{sha256_hex}.lock"
    ipod_path: Path          # Path(os.path.realpath(ipod_path))
```

### 2.2 Construction (no side effects yet)

1. `self.ipod_path = Path(os.path.realpath(ipod_path))`.
2. **Writer-key normalization** (`_writer_lock_identity`): the identity string is
   `volume_key.strip()` if `volume_key` is truthy, otherwise `str(self.ipod_path).strip()`.
   Split on `"|"`; if there are **exactly 4 parts and the first three are all
   non-empty**, only the first three are re-joined (the fourth field — the
   mount-instance id — is dropped so remounts of the same underlying volume share
   one lock). Otherwise the string is unchanged.
   `self._writer_key = hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()`.
3. Lock base directory: `lock_dir` argument if given; otherwise
   `Path(tempfile.gettempdir()) / "podsync-device-locks"` with `-{getuid()}` appended
   to the directory name when `os.getuid` exists.
4. `self.lock_path = base_dir / f"{self._writer_key}.lock"`.
5. Remaining constructor state: `_file=None`, `_entered=False`,
   `_in_process_reserved=False`, `_database_generation=None`, plus the three
   keyword flags stored as-is (`_track_database_generation=bool(...)`,
   `_queue_in_process=bool(...)`).

Module-level process state (shared by all guards):

| Name | Type | Purpose |
|---|---|---|
| `_ACTIVE_WRITERS` | `set[str]` | writer keys currently held in-process |
| `_ACTIVE_WRITER_THREADS` | `dict[str, int]` | writer key → owning `threading.get_ident()` |
| `_WAITING_WRITERS` | `dict[str, deque[object]]` | per-key FIFO ticket queues |
| `_ACTIVE_WRITERS_CONDITION` | `threading.Condition` | guards all of the above |

### 2.3 `__enter__` — arming sequence (exact order)

Entering twice on the same instance raises
`RuntimeError("DeviceWriteGuard instances cannot be entered twice")`.

1. **In-process reservation** (`_reserve_in_process`), executed *before* touching
   any file:
   * If `_ACTIVE_WRITER_THREADS[key] == current thread` → raise
     `DeviceBusyError("This podsync operation is already writing to this iPod. Nested device write sessions are not supported.")`.
   * If `queue_in_process=False`: if `key in _ACTIVE_WRITERS` → raise
     `DeviceBusyError("podsync is already writing to this location. Wait for that operation to finish, then try again.")`;
     otherwise add key + thread id, set `_in_process_reserved=True`, return.
   * If `queue_in_process=True` (default): append a fresh ticket object to the
     per-key deque, then `wait()` on the condition until the key is **not** active
     *and* the ticket is at the front of the deque. Then pop the ticket (delete
     the deque entry if empty), mark active, record thread id, set
     `_in_process_reserved=True`. **Consequence: in-process writers queue in FIFO
    order and block; only same-thread nesting and `queue_in_process=False` fail
    fast with `DeviceBusyError`.** On any `BaseException` during the wait, remove
    the ticket if still queued, drop the empty deque, `notify_all()`, re-raise.
2. `_ensure_safe_lock_directory(lock_path.parent)`:
   * `mkdir(mode=0o700, parents=True, exist_ok=True)`, then `lstat()`.
   * Any `OSError` → `DeviceWriteSafetyError("Could not create the host-side iPod lock directory safely: {exc}")`.
   * If the entry is a symlink (`stat.S_ISLNK`) or has
     `st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT` (0x400) or is not a
     directory → `DeviceWriteSafetyError("The host-side iPod lock directory is a link, reparse point, or non-directory. podsync stopped before writing.")`.
   * On POSIX (`os.geteuid` callable): owner must equal `geteuid()` else
     `DeviceWriteSafetyError("...owned by another user...")`; then
     `os.chmod(path, 0o700)`, and failure to chmod raises
     `DeviceWriteSafetyError("Could not secure the host-side iPod lock directory: {exc}")`.
3. `_open_lock_file_safely(lock_path)`:
   * **POSIX:** `os.open(path, O_RDWR|O_CREAT|O_CLOEXEC|O_NOFOLLOW|O_NONBLOCK, 0o600)`
     (the `O_CLOEXEC`/`O_NOFOLLOW`/`O_NONBLOCK` bits are added only if the
     constants exist). `OSError` → `DeviceWriteSafetyError("Could not open the host-side iPod lock file safely: {exc}")`.
     Then `fstat`: must be a regular file (else
     `DeviceWriteSafetyError("...not a regular file...")`); on POSIX owner must
     equal `geteuid()` (else `...owned by another user...`); `os.fchmod(fd, 0o600)`;
     wrap with `os.fdopen(fd, "r+b")`. Any check failure closes the fd first.
     **A symlink at `lock_path` therefore raises `DeviceWriteSafetyError` and the
     link's target is never opened or modified.**
   * **Windows:** `CreateFileW(path, GENERIC_READ|GENERIC_WRITE, FILE_SHARE_READ|FILE_SHARE_WRITE, ..., OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL|FILE_FLAG_OPEN_REPARSE_POINT, ...)`.
     Invalid handle → `DeviceWriteSafetyError("Could not open the host-side iPod lock file safely: {WinError}")`.
     `GetFileInformationByHandle`; if attributes include directory (0x10) or
     reparse (0x400) → `DeviceWriteSafetyError("The host-side iPod lock path is a link, reparse point, or non-file. podsync stopped before writing.")`.
     Otherwise `msvcrt.open_osfhandle(..., O_RDWR|O_BINARY)` → `os.fdopen(..., "r+b")`.
     Any other `OSError` in this sequence →
     `DeviceWriteSafetyError("Could not verify the host-side iPod lock file safely: {exc}")`.
     The handle is closed in `finally` unless ownership transferred.
4. `_ensure_lock_byte(file)`: seek to end; if size 0 write `b"\0"` and flush;
   seek back to 0 (the OS lock covers byte 0).
5. `_lock_file_nonblocking(file)`:
   * Windows: `file.seek(0)` then `msvcrt.locking(fileno, msvcrt.LK_NBLCK, 1)`.
   * POSIX: `fcntl.flock(fileno, LOCK_EX | LOCK_NB)`.
   * `OSError` → `DeviceBusyError("Another podsync process is already writing to this iPod. Wait for that operation to finish, then try again.")`
     (substring `already writing` must appear).
6. `_write_owner_metadata()`: `seek(1)`, `truncate()`, write
   `f"pid={os.getpid()} mount={self.ipod_path}\n".encode("utf-8", errors="replace")`,
   `flush()`. (Byte 0 stays the lock byte.)
7. **Generation snapshot:** if `track_database_generation` →
   `current = capture_database_generation(self.ipod_path)`, else `current = None`.
   * If `expected_database_generation` was supplied and `current is None` →
     `DeviceWriteSafetyError("The expected iPod database generation cannot be verified while database tracking is disabled. podsync stopped before writing.")`.
   * If supplied and `not _same_database_generation(current, expected)` →
     `ExternalDatabaseChangeError("The iPod database changed since the iPod library was loaded. podsync stopped before overwriting those newer changes. Reload the iPod library and try again.")`
     — required test substring: **`changed since the iPod library was loaded`**
     (keep this sentence shape verbatim apart from the application name).
   * Store `self._database_generation = current`.
8. `self._entered = True`, `logger.debug("Acquired exclusive iPod writer guard: mount=%s lock=%s database=%s", ...)` (DEBUG only), return `self`.

**Failure atomicity:** any exception anywhere inside `__enter__` runs
`_release_resources()` (unlock + close the file if opened, release the
in-process reservation, `notify_all()`) and then re-raises. No lock, queue slot,
or fd leaks on failure.

### 2.4 `__exit__` — release

`_release_resources()` in this order:

1. Take `self._file`, set `self._file = None`. If it was open:
   `_unlock_file` (Windows: `seek(0)` + `msvcrt.locking(..., LK_UNLCK, 1)`;
   POSIX: `flock(LOCK_UN)`). `OSError` during unlock →
   `logger.warning("Could not release iPod writer lock cleanly: %s", exc)`
   — never raises. Always `close()` the file in `finally`.
2. If `_in_process_reserved`: under the condition lock, discard the key from
   `_ACTIVE_WRITERS`, pop `_ACTIVE_WRITER_THREADS`, clear the flag, `notify_all()`
   (wakes the next queued ticket).
3. If `_entered`: `logger.debug("Released exclusive iPod writer guard: ...")`.
   Set `_entered = False`.

The OS advisory lock is per-process/fd, so a crashing process releases it
automatically; the on-disk file is never deleted.

### 2.5 Generation methods

* `starting_database_generation` — returns `_database_generation`, or raises
  `RuntimeError("Device write guard is not active")` when it is `None`. It is
  `None` both before successful entry **and after successful entry when
  `track_database_generation=False`** (documented edge case: with tracking off,
  this property always raises).
* `assert_database_unchanged()` — obtains `starting_database_generation`
  (so it also raises `RuntimeError` when tracking is off), re-captures
  `capture_database_generation(self.ipod_path)`; if equal → return silently;
  else raise `ExternalDatabaseChangeError("The iPod database changed after this write session started. podsync stopped before overwriting the newer database. Reload the iPod library and try again after closing other device-management apps.")`
  (required substring: `changed after this write session started`).
* `refresh_database_generation()` — if not `_entered` → `RuntimeError("Device write guard is not active")`;
  else re-capture and store (used after this session's own commit so the next
  `assert_database_unchanged` compares against the new file).

### 2.6 `capture_database_generation(ipod_path) -> DatabaseGeneration`

1. Resolve the DB path with **`podsync.device.info`** authority:
   `resolve_itdb_path(str(ipod_path))`; if `None`, fall back to
   `ipod_path / "iPod_Control" / "iTunes" / itdb_write_filename(str(ipod_path))`
   (Chapter 06 semantics: zero-byte alternate-name markers are ignored, a known
   Classic tracks `iTunesDB` even when a stale non-empty `iTunesCDB` exists, etc.).
2. `database.stat()`:
   * `FileNotFoundError` → `DatabaseGeneration(filename=database.name, exists=False)`
     (all other fields defaults). *No hashing.*
   * Other `OSError` → `DeviceWriteSafetyError("Could not inspect the iPod database before writing: {exc}")`.
3. Stream SHA-256 in **1 MiB chunks** (`_HASH_CHUNK_SIZE = 1024 * 1024`), then
   `stat()` again. `OSError` anywhere →
   `DeviceWriteSafetyError("Could not read the iPod database generation safely: {exc}")`.
4. Compare the tuples `(st_size, st_mtime_ns, st_dev, st_ino)` from before and
   after the hash. Any difference →
   `ExternalDatabaseChangeError("The iPod database changed while podsync was inspecting it. Close other device-management apps, reload the library, and try again.")`.
5. Return `DatabaseGeneration(filename, exists=True, size, modified_ns=st_mtime_ns,
   device=st_dev, inode=st_ino, digest=hexdigest)`.

`_same_database_generation(left, right)`: if **both** have `exists=False` →
`True` (a later exact device identification may legitimately switch the expected
filename from `iTunesDB` to `iTunesCDB` while no file exists); otherwise plain
dataclass equality (all fields, including `digest`).

### 2.7 Behavioral checklist

| Scenario | Required behavior |
|---|---|
| Lock path is a symlink to a victim file | `DeviceWriteSafetyError` mentioning "lock"; victim bytes unchanged |
| `track_database_generation=False` | `capture_database_generation` must not be called at all |
| Successful guard at `INFO` level | zero log records |
| Two guards, same key, same process (sequential) | second acquires after first exits |
| Two threads, same key, `queue_in_process=True` | second **blocks** until first releases; acquisition order = start order |
| `volume_key="linux|8:33|/dev/sdb1|77"` vs `"...|91"` | collide (4th field dropped) → `DeviceBusyError` with `already writing` |
| DB rewritten inside session | `assert_database_unchanged()` → `ExternalDatabaseChangeError` |
| DB created (absent → present) inside session | `ExternalDatabaseChangeError` (exists flag flip) |
| Session's own commit + `refresh_database_generation()` | subsequent assert passes |
| Cached generation replaced before `__enter__` | `ExternalDatabaseChangeError` at entry |
| Re-entry of same instance | `RuntimeError` |

---

## 3. `podsync.device.path_safety`

### 3.1 API

```python
class UnsafeDevicePathError(ValueError): ...
class UnsafeHostPathError(ValueError): ...

def resolve_device_path(
    ipod_root: str | Path,
    device_relative_path: str | Path,
    *,
    allowed_subtree: str | Path,
) -> Path: ...

def resolve_host_path(
    allowed_root: str | Path,
    persisted_path: str | Path,
) -> Path: ...
```

### 3.2 Relative-part validation (`_validated_relative_parts`, shared)

Applied to **both** `device_relative_path` and `allowed_subtree`. Given
`raw = os.fspath(value)`:

1. Not a `str`, empty, or contains `"\x00"` → `UnsafeDevicePathError("Invalid {label}")`
   (`label` is `"device path"` or `"allowed subtree"`).
2. Normalize separators: `unified = raw.replace("\\", "/")`.
3. Absolute forms rejected: `unified.startswith("/")` **or** `re.match(r"^[A-Za-z]:", unified)`
   → `UnsafeDevicePathError("{label capitalized} must be relative")`.
   This catches `/etc/passwd`, `\\server\share\...` (becomes `//server/...`),
   `C:\Users\...`, and the drive-relative `C:Music\...`.
4. Split on `/`; any component that is empty, `"."`, `".."`, or contains `":"`
   → `UnsafeDevicePathError("Invalid component in {label}: {raw}")`.
   (Colon components emulate Windows drive/ADS syntax and are always rejected —
   this is the "reserved names" rule at this layer.)
5. Return `tuple(parts)`.

### 3.3 `resolve_device_path` algorithm

1. `relative_parts = _validated_relative_parts(device_relative_path, "device path")`;
   `allowed_parts = _validated_relative_parts(allowed_subtree, "allowed subtree")`.
2. `root = Path(ipod_root).resolve(strict=False)`.
3. **Lexical containment:** `allowed_lexical = root.joinpath(*allowed_parts)`,
   `candidate_lexical = root.joinpath(*relative_parts)`.
   * `allowed_lexical.is_relative_to(root)` must hold, else
     `UnsafeDevicePathError("Allowed iPod subtree resolves outside the device root")`.
   * `candidate_lexical.is_relative_to(allowed_lexical)` must hold, else
     `UnsafeDevicePathError(f"Device path is outside the allowed iPod subtree: {device_relative_path}")`.
4. **Link rejection walk:** starting at `root`, `lstat()` each successive
   component of `relative_parts`:
   * `FileNotFoundError` → component doesn't exist yet: **continue** (writes may
     target not-yet-created files).
   * Other `OSError` → `UnsafeDevicePathError("Could not safely inspect device path component {current}: {exc}")`.
   * Symlink mode (`stat.S_ISLNK`) **or** `st_file_attributes & 0x400`
     (Windows reparse point) →
     `UnsafeDevicePathError("Device path contains a symbolic link or reparse point: {current}")`.
   The walk covers the *final* component too — a symlink at the target filename
   is rejected.
5. **Resolved containment:** `allowed = allowed_lexical.resolve(strict=False)`
   must still be `is_relative_to(root)` (same error as step 3a);
   `candidate = candidate_lexical.resolve(strict=False)` must be
   `is_relative_to(allowed)` (same message as step 3b). This catches symlinks
   anywhere inside the *allowed subtree's own path* even if the candidate walk
   could not see them.
6. Return `candidate` (the fully resolved absolute `Path`).

**Summary of raising inputs** (all → `UnsafeDevicePathError`):

| Input class | Example | Stage |
|---|---|---|
| empty / non-str / NUL | `""`, `"a\x00b"` | validation |
| absolute POSIX | `/etc/passwd` | validation |
| absolute/UNC Windows | `C:\x`, `\\srv\share\x` | validation |
| drive-relative | `C:Music\song` | validation |
| parent traversal | `a/../../outside`, `..` component | validation |
| `.` or empty component | `a//b`, `a/./b` | validation |
| colon component | `a:b` | validation |
| outside allowed subtree (lexical) | `Photos/...` under Music subtree | step 3b |
| symlink/reparse in any component incl. leaf | `Music/F00 -> /elsewhere` | step 4 |
| allowed subtree escaping root after resolve | — | step 5 |
| candidate escaping allowed after resolve | — | step 5 |

Note: a directory link that stays **inside** the allowed subtree (e.g.
`Music/F00 -> Music/F01`) is still rejected by step 4 — *any* link component is
refused, not only escaping ones.

### 3.4 `resolve_host_path(allowed_root, persisted_path)`

Trust anchor is the configured root; the persisted value must be an absolute
host path.

1. `raw = os.fspath(persisted_path)`; not `str` / empty / contains NUL →
   `UnsafeHostPathError("Invalid persisted host path")`.
2. `os.path.isabs(raw)` must hold → else
   `UnsafeHostPathError("Persisted host path must be absolute")`.
3. `root = Path(os.path.abspath(allowed_root))`,
   `candidate_lexical = Path(os.path.abspath(raw))`.
   `candidate_lexical.relative_to(root)` must succeed, else
   `UnsafeHostPathError(f"Persisted host path is outside the allowed root: {raw}")`.
4. `relative.parts` must be non-empty → else
   `UnsafeHostPathError("Persisted host path must name a file below the root")`
   (the root directory itself is not an acceptable file path).
5. Same lstat link/reparse walk as §3.3 step 4 over `relative.parts` from `root`,
   raising `UnsafeHostPathError` with `path_label="Host path"`
   (messages: `"Could not safely inspect host path component ..."` /
   `"Host path contains a symbolic link or reparse point: ..."`).
6. Resolve root and candidate with `strict=False`; candidate must be
   `is_relative_to(resolved_root)`, else
   `UnsafeHostPathError(f"Persisted host path escapes the allowed root: {raw}")`.
7. Return resolved candidate.

### 3.5 Interoperation (Chapter for `sync` / track paths)

`podsync.sync.ipod_track_paths` (specified elsewhere) normalizes iTunesDB colon
locations (`:iPod_Control:Music:F00:Song.mp3`), Windows drive locations, absolute
host paths containing `iPod_Control`, and `file://` URIs into a device-relative
path and then calls `resolve_device_path(root, ..., allowed_subtree="iPod_Control/Music")`.
Consequences this chapter guarantees to those callers:

* All "unsafe" returns there stem from `UnsafeDevicePathError` (a `ValueError`);
  helpers that return `Optional[Path]` convert the exception to `None`, while
  `ipod_location_from_file_path` re-raises `UnsafeDevicePathError` with its own
  message *"Track path is outside the iPod music directory: ..."*
  (the phrase `outside the iPod music` belongs to that helper, not to this module).
* Colon locations with traversal (`:...:..:..:outside.mp3`) fail at validation;
  NULs fail at validation; external absolute locations without an `iPod_Control`
  marker are normalized away by the caller *before* reaching this module, and
  rejected here if they arrive raw.

---

## 4. `podsync.device.storage_safety`

### 4.1 API

```python
class FileSizeLimitError(DeviceWriteSafetyError):
    proposed_database_bytes: bytes      # __init__ sets b""
    proposed_database_filename: str     # __init__ sets ""
    def __init__(self, message: str) -> None: ...

def allocated_size(logical_size: int, allocation_unit_size: int | None) -> int: ...
def existing_file_allocated_size(path: str | Path, allocation_unit_size: int | None) -> int: ...
def effective_max_file_size_bytes(
    filesystem_limit: int | None, device_limit: int | None
) -> int | None: ...
def require_file_size_supported(
    file_size: int,
    *,
    max_file_size_bytes: int | None,
    display_name: str,
) -> None: ...
```

### 4.2 Rules

**`allocated_size(logical, unit)`** — conservative on-disk bytes for one logical
file:

| call | result | why |
|---|---|---|
| `allocated_size(0, 4096)` | `0` | empty file |
| `allocated_size(1, 4096)` | `4096` | one cluster |
| `allocated_size(4097, 4096)` | `8192` | round **up** |
| `unit <= 1` or missing | `logical` (clamped ≥ 0) | unknown unit ⇒ logical size |

Formula: sizes/units are coerced with `max(0, int(x or 0))`; if `size == 0` or
`unit <= 1` return `size`, else `((size + unit - 1) // unit) * unit`.

**`existing_file_allocated_size(path, unit)`** — bytes a *deletion* can safely be
assumed to free (sparse/compressed files occupy less than logical size):

1. `file_stat = Path(path).stat()` (propagates `OSError`/`FileNotFoundError`).
2. If `getattr(file_stat, "st_blocks", None)` is an `int ≥ 0` → return
   `st_blocks * 512`. (POSIX always takes this branch; the `unit` argument is
   accepted for API symmetry and is **not** used in the computation.)
3. Else on `win32`: call `kernel32.GetCompressedFileSizeW` (64-bit compose of
   high/low words). `low == 0xFFFFFFFF and GetLastError() != 0` → raise
   `OSError(code, FormatError(code))`. Otherwise return the composed value.
4. Anywhere else: return `0` — *never over-promise* space to a destructive
   preflight.

**`effective_max_file_size_bytes(filesystem_limit, device_limit)`** — collect all
`None`-free, strictly positive `int` values among the two inputs; return their
`min`, or `None` when the collection is empty.
`(4000, 3000) → 3000`; `(4000, None) → 4000`; `(None, None) → None`;
non-positive values are ignored as if absent.

**`require_file_size_supported(file_size, *, max_file_size_bytes, display_name)`**:

* `size = max(0, int(file_size or 0))`, `limit = int(max_file_size_bytes or 0)`.
* `limit <= 0` **or** `size <= limit` → return (no-op; an unknown limit never
  blocks).
* Otherwise `logger.debug("File-size safety guard rejected write: display_name=%s file_size_bytes=%d max_file_size_bytes=%d", ...)` —
  the log line must contain the raw byte values (tests match
  `file_size_bytes=34603008 max_file_size_bytes=33554432`), then raise
  `FileSizeLimitError(f"{display_name} is {_format_size(size)}, exceeding the {_format_size(limit)} maximum supported by this iPod or its filesystem. podsync stopped before writing the file.")`.

  Required substrings in the message: `"{display_name} is"` and both formatted
  sizes; e.g. `album.flac is 5.0 GB, exceeding the 4.0 GB maximum ...`,
  `iTunesDB is 33.0 MB, exceeding the 32.0 MB maximum ...`.

**`_format_size(size)`** (exact thresholds):

| condition | format | example |
|---|---|---|
| `size >= 0.1 * 1024**3` | `f"{size / 1024**3:.1f} GB"` | `4.0 GB` |
| `size >= 1024**2` | `f"{size / 1024**2:.1f} MB"` | `33.0 MB` |
| otherwise | `f"{size / 1024:.1f} KB"` | `1.5 KB` |

**Protected sizes on device:** the *filesystem-derived* limit comes from
`FilesystemProfile.max_file_size_bytes` (§6 table: FAT12/16 → 2 GiB − 1, FAT32
family → 4 GiB − 1), the *device-derived* database limit from
`DeviceCapabilities.max_database_bytes` (chapter 06 §4; there is no per-file
device limit); callers combine them with
`effective_max_file_size_bytes` and enforce with `require_file_size_supported`
**before** any temp file is created, so an over-limit database write aborts with
the original file intact.

---

## 5. `podsync.device.filesystem`

### 5.1 API and constants

```python
ITUNESDB_PLATFORM_MAC = 1
ITUNESDB_PLATFORM_WINDOWS = 2

@dataclass(frozen=True, slots=True)
class ITunesDBPlatformResolution:
    flag: int
    source: str                       # "existing_database" | "filesystem" | "default"
    filesystem_type: str              # normalized
    inferred_flag: int | None
    reference_platform: int | None
    mismatch: bool

def detect_filesystem_type(mount_path: str | Path) -> str: ...
def filesystem_itunesdb_platform(filesystem_type: str) -> int | None: ...
def resolve_itunesdb_platform(
    *, filesystem_type: str, reference_platform: int | None
) -> ITunesDBPlatformResolution: ...
```

Normalization everywhere: `_normalize_filesystem_type(v) = str(v or "").strip().casefold()`.

### 5.2 Platform membership (FAT/exFAT detection logic)

| Group | Set (after normalization) | `filesystem_itunesdb_platform` |
|---|---|---|
| Mac | `apfs, hfs, hfs+, hfsplus, hfsx` | `1` |
| Windows | `exfat, fat, fat16, fat32, msdos, msdosfs, ntfs, vfat` | `2` |
| anything else / empty | — | `None` |

Note: **exFAT and NTFS map to the Windows flag here**, but see §7 — they are
*not* in the write-supported set for physical iPods, so readiness rejects them
before any write.

### 5.3 `detect_filesystem_type(mount_path) -> str`

Per-platform detection; empty string on any failure (never raises for detection
failure):

| Platform | Primary method | Fallback | Timeout |
|---|---|---|---|
| Linux (`linux*`) | `findmnt -n -o FSTYPE --target <path>` (stdout first line, normalized) | scan `/proc/self/mounts`: fields split, `len(parts) >= 3`, decode octal escapes `\nnn` in `parts[1]`, exact string equality with `mount_path`, return normalized `parts[2]`; `OSError` → debug log | 5 s |
| macOS | `diskutil info -plist <path>` → plist dict keys in order `FilesystemType`, `FilesystemName`, `FilesystemPersonality` (first non-empty, normalized) | — (`FileNotFoundError`/timeout/invalid plist → debug log → `""`); non-zero exit → `""` | 5 s |
| Windows | `GetVolumeInformationW` on the drive root (`{drive}\` from `os.path.splitdrive`, else the path's anchor) into a 256-char buffer | — (`AttributeError`/`OSError` → debug log → `""`; API failure → `""`) | — |
| other | `""` | — | — |

### 5.4 `resolve_itunesdb_platform`

Precedence (filesystem is only a *fallback*):

1. `reference_platform in (1, 2)` → `flag=reference`, `source="existing_database"`.
2. Else inferred from `filesystem_itunesdb_platform(normalized_fs)` →
   `flag=inferred`, `source="filesystem"`.
3. Else `flag=2` (Windows), `source="default"`.
4. `reference_platform` recorded in the resolution is `None` unless it was 1 or 2
   (invalid values such as `0` behave as "no reference").
5. `mismatch = reference is not None and inferred is not None and reference != inferred`.

Examples: `(filesystem_type="hfsplus", reference_platform=2)` →
`flag=2, source="existing_database", inferred_flag=1, mismatch=True`;
`(filesystem_type="hfsplus", reference_platform=0)` →
`flag=1, source="filesystem", inferred_flag=1, mismatch=False`.

---

## 6. `podsync.device.filesystem_profile`

### 6.1 Data types

```python
@dataclass(frozen=True, slots=True)
class VolumeIdentity:
    operating_system: str
    device_id: str
    volume_id: str
    mount_instance: str
    @property
    def is_complete(self) -> bool: ...   # all four fields non-empty

@dataclass(frozen=True, slots=True)
class FilesystemProfile:
    mount_path: str
    filesystem_type: str
    reported_volume_format: str
    mount_source: str
    mount_options: tuple[str, ...]
    read_only: bool
    unsafe_write_reasons: tuple[str, ...]
    case_sensitive: bool | None
    max_file_size_bytes: int | None
    max_component_length: int | None
    allocation_unit_size: int | None
    identity: VolumeIdentity
    detection_errors: tuple[str, ...]
    inspection_path: str = ""            # default; equals realpath(mount) at inspect time

    @property
    def safe_for_writes(self) -> bool: ...
        # not read_only AND not unsafe_write_reasons
        # AND filesystem_type != "" AND identity.is_complete

@dataclass(frozen=True, slots=True)
class FilesystemRevalidation:
    safe_to_continue: bool
    failure_code: str
    reason: str
    current_profile: FilesystemProfile
    @property
    def current_identity(self) -> VolumeIdentity: ...
```

Module constants:

* `_LINUX_MOUNTINFO = Path("/proc/self/mountinfo")`,
  `_LINUX_UDEV_DATA = Path("/run/udev/data")` (tests monkeypatch these).
* `_MAX_FILE_SIZE_BYTES`:

| filesystem_type | max file bytes |
|---|---|
| `fat`, `fat16` | `2 * 1024**3 - 1` (2 GiB − 1) |
| `fat32`, `msdos`, `msdosfs`, `vfat` | `4 * 1024**3 - 1` (4 GiB − 1) |
| anything else (exfat, ntfs, hfs*, unknown) | `None` |

### 6.2 `inspect_filesystem_profile(mount_path, *, reported_volume_format="", probe_case_sensitivity=False) -> FilesystemProfile`

1. `requested_path = os.path.realpath(mount_path)`.
2. Collect `_MountedFilesystemFacts` per platform (details below). Unsupported
   platform → `_unavailable_facts(requested_path)` (os=`"unknown"`).
3. `filesystem_type = facts.filesystem_type or detect_filesystem_type(requested_path)`
   (§5.3 fallback).
4. If `max_component_length is None`: `os.pathconf(requested_path, "PC_NAME_MAX")`
   → int; failure (`AttributeError`/`OSError`/`ValueError`, including platforms
   without `os.pathconf`) appends
   `"Could not determine maximum filename length: {exc}"` to `detection_errors`.
5. If `allocation_unit_size is None`: `os.statvfs(requested_path)` →
   `int(f_frsize or f_bsize)`; failure appends
   `"Could not determine allocation unit size: {exc}"`.
6. `unsafe_write_reasons = _unsafe_write_reasons(...)`:

   | condition | reason tuple |
   |---|---|
   | `operating_system == "linux"` **and** `filesystem_type in {hfs, hfs+, hfsplus, hfsx}` **and** `"force" in mount_options` | `("Linux HFS volume is mounted with the unsafe 'force' option",)` |
   | otherwise | `()` |

7. **Case-sensitivity probe** (only when `probe_case_sensitivity=True` **and**
   `can_probe_safely`, where
   `can_probe_safely = not read_only and not unsafe_reasons and filesystem_type != "" and identity.is_complete`):
   * Probe directory `_case_probe_directory(requested)`:
     `requested/iPod_Control/iTunes` if that is a directory, else `requested`
     itself (so virtual iPods nested below a host mount probe their own subtree).
   * `_probe_case_sensitivity(dir)`:
     - `tempfile.mkstemp(prefix=".Podsync_CaseProbe_Aa_", dir=dir)`, close fd,
       `alternate = probe.with_name(probe.name.swapcase())`,
       `case_sensitive = not alternate.exists()` (exists ⇒ case-insensitive ⇒ `False`).
     - `OSError` at any point → `error = "Could not probe filesystem case sensitivity: {exc}"`, `case_sensitive` stays `None`.
     - `finally`: close a still-open fd; unlink the probe with
       `missing_ok=True`; unlink failure ⇒ `case_sensitive = None`,
       `error = "Could not remove filesystem case probe: {exc}"`.
     - Returns `(case_sensitive, error)`.
   * On error: append to `detection_errors` **and**
     `unsafe_write_reasons += ("Filesystem case sensitivity could not be verified",)`.
   * When not requested or not safe: `case_sensitive = None`, no file I/O.
8. Build and return the profile:
   `max_file_size_bytes=_MAX_FILE_SIZE_BYTES.get(filesystem_type)`,
   `reported_volume_format=str(reported_volume_format or "").strip()`,
   `inspection_path=requested_path`.

### 6.3 Per-OS fact collection

**Linux** (`_inspect_linux`):

1. Read `_LINUX_MOUNTINFO` text; `OSError` → unavailable facts (os=`"linux"`) with
   error `"Could not read Linux mount table: {exc}"`.
2. Parse each line: `fields = line.split()`; must contain `"-"` at index
   `separator ≥ 6` with `len(fields) > separator + 3`. From the pre-separator
   fields: mount id `fields[0]`, device `fields[2]`, mount point
   `realpath(_decode_mountinfo_field(fields[4]))`; after `-`: fstype
   `fields[separator+1].strip().casefold()`, source
   `_decode_mountinfo_field(fields[separator+2])`. Mount options =
   `_unique_options(fields[5], fields[separator+3])` (split each on `,`,
   drop empties, preserve first-seen order). `read_only = "ro" in mount_options`.
   Identity = `("linux", device_id=fields[2], volume_id=source, mount_instance=fields[0])`.
   Escape decoding: `\\040→space`, `\\011→tab`, `\\012→newline`, `\\134→backslash`.
3. Select the entry whose (rstrip-sep) mount point equals `requested_path` or is
   a `os.sep`-boundary prefix of it — **longest mount point wins**.
4. If a best entry exists: try `_linux_filesystem_uuid(device_id)` — only when
   `device_id` fullmatches `\d+:\d+`, read `_LINUX_UDEV_DATA / f"b{device_id}"`
   and take the line `E:ID_FS_UUID=...`; if found, replace
   `identity.volume_id` with `f"uuid:{uuid}"`. Return those facts.
5. No match → unavailable facts (os=`"linux"`) with error
   `"No Linux mount contains {requested_path}"`.

**macOS** (`_inspect_macos`) — run `["diskutil", "info", "-plist", requested_path]`
(timeout 5 s):

| failure | error string (facts otherwise unavailable, os=`"macos"`) |
|---|---|
| `FileNotFoundError`/`OSError`/timeout | `Could not inspect macOS volume: {exc}` |
| non-zero exit | `diskutil info failed with exit code {code}` |
| plist parse error | `Could not parse diskutil output: {exc}` |
| non-dict plist | `diskutil returned an unexpected response` |

Success facts:
`mount_path = realpath(info["MountPoint"] or requested_path)`;
`filesystem_type = (FilesystemType or FilesystemName or FilesystemPersonality).strip().casefold()`;
`mount_source = f"/dev/{DeviceIdentifier}"` (else `""`);
options: string → split commas; list/tuple → stripped non-empty strings; other → `()`;
`read_only = (Writable is False) or (VolumeReadOnly is True) or (ReadOnlyVolume is True)`;
`allocation_unit_size = positive int of AllocationBlockSize`;
identity `("macos", device_id=DeviceIdentifier.strip(),
volume_id = first non-empty of VolumeUUID/DiskUUID/MediaUUID, mount_instance=device_id)`.

**Windows** (`_inspect_windows`) — root = `{drive}\` (drive from
`os.path.splitdrive(os.path.abspath(path))`) else path anchor:

1. `GetVolumeInformationW(root, None, 0, &serial, &max_component, &flags, fs_name[256], 256)`.
   Failure/`AttributeError`/`OSError` → unavailable facts (os=`"windows"`) with
   error `"Could not inspect Windows volume: {exc}"` / `"GetVolumeInformationW failed"`.
2. `GetVolumeNameForVolumeMountPointW(root, vol_name[1024], ...)` → if success
   `mount_source/volume_device = vol_name.value` (e.g.
   `\\?\Volume{GUID}\`), else `root` (the drive letter root).
3. Allocation unit: `GetDiskFreeSpaceW` → `sectors_per_cluster * bytes_per_sector`
   (≤ 0 → `None`).
4. Facts: `mount_path = root`; `filesystem_type = fs_name.value.strip().casefold()`;
   `mount_options = ()`; **`read_only = bool(flags & 0x00080000)`**
   (`FILE_READ_ONLY_VOLUME`); `max_component_length = positive int(max_component)`;
   identity `("windows", device_id=volume_device, volume_id=f"{serial:08X}",
   mount_instance=volume_device)`.

**Unavailable facts template:** `mount_path=requested`, `filesystem_type=""`,
`mount_source=""`, `mount_options=()`, `read_only=False`,
identity `(operating_system=<per-OS label>, "", "", "")`.

### 6.4 `revalidate_filesystem_profile(retained, *, probe_case_sensitivity=None)`

1. `should_probe_case = probe_case_sensitivity is True`.
2. `current = inspect_filesystem_profile(
   retained.inspection_path or retained.mount_path,
   reported_volume_format=retained.reported_volume_format,
   probe_case_sensitivity=should_probe_case)`.
3. If **not** probing now but `retained.case_sensitive is not None` → carry the
   retained value into `current` (a previously observed result is not revoked by
   a revalidation that didn't re-probe).
4. Fail-closed checks in this exact order (first hit wins):

   | # | Condition | `failure_code` | `reason` |
   |---|---|---|---|
   | 1 | `retained.identity` incomplete | `identity_unavailable` | `The original volume identity was incomplete and cannot be revalidated.` |
   | 2 | `current.identity` incomplete | `identity_unavailable` | `The mounted volume identity could not be read.` |
   | 3 | `mount_instance` differs | `mount_changed` | `The volume mount instance changed after inspection.` |
   | 4 | full identity differs | `volume_changed` | `A different volume is mounted at the inspected path.` |
   | 5 | `filesystem_type` differs | `filesystem_changed` | `The mounted filesystem type changed after inspection.` |
   | 6 | `current.read_only` | `read_only` | `The mounted volume is now read-only.` |
   | 7 | `current.unsafe_write_reasons` | `unsafe_mount` | `current.unsafe_write_reasons[0]` |
   | — | none | `""` (with `reason=""`) | success: `FilesystemRevalidation(True, "", "", current)` |

   Case-sensitivity changes alone never fail revalidation.

---

## 7. `podsync.device.write_readiness`

### 7.1 API

```python
def inspect_device_write_readiness(
    mount_path: str | Path,
    *,
    reported_volume_format: str = "",
) -> FilesystemProfile: ...

def revalidate_device_write_readiness(
    retained_profile: FilesystemProfile,
    *,
    probe_case_sensitivity: bool | None = None,
) -> FilesystemProfile: ...

def volume_lock_key(profile: FilesystemProfile) -> str: ...
```

Supported physical filesystem set (exact frozenset):
`{"fat", "fat16", "fat32", "hfs", "hfs+", "hfsplus", "hfsx", "msdos", "msdosfs", "vfat"}`.

### 7.2 `inspect_device_write_readiness` — the write gate

1. `profile = inspect_filesystem_profile(mount_path,
   reported_volume_format=reported_volume_format, probe_case_sensitivity=False)`
   — note: **no case probe at initial inspection** (it happens inside the guarded
   session revalidation instead).
2. `requested = os.path.normcase(os.path.realpath(mount_path))`;
   `profile = virtual_ipod_profile(profile, requested)`
   (Chapter 06: when `iPodInfo.json` is a regular file at that realpath, the
   profile's `mount_path`/`inspection_path` become the virtual root and
   `identity.operating_system == "virtual"` with a synthesized, revalidatable
   identity; host read-only/unsafe facts remain intact).
3. `is_virtual = profile.identity.operating_system == "virtual"`.
4. `_log_unsafe_profile("inspected", profile)` — **no log when safe**; when
   unsafe, one `WARNING` beginning
   `"Unsafe iPod filesystem profile inspected: selected=... mount=... actual=... reported=... source=... options=... read_only=... case_sensitive=... max_file_bytes=... max_name=... allocation_unit=... identity=os/device/volume/instance safe_for_writes=... errors=..."`.
5. `_log_reported_format_mismatch(profile)` — `WARNING`
   `"iPod filesystem/report mismatch: actual=%s reported=%s"` when both
   actual and reported formats map via `filesystem_itunesdb_platform` and the
   two platform flags differ.
6. Checks, in order (first failure raises `DeviceWriteSafetyError`):

   | # | Check (skipped for virtual) | Message (must appear verbatim modulo the §Intro rebrand) |
   |---|---|---|
   | 1 | `requested != normcase(realpath(profile.mount_path))` | `The selected iPod path is not mounted as its own volume. Selected path: {requested}; containing mount: {observed}. podsync stopped to avoid writing into an empty host directory.` (substring `not mounted`) |
   | 2 | `(Path(requested) / "iPod_Control").is_dir()` false | `The selected volume does not contain an iPod_Control directory. podsync stopped before writing to an unrecognized volume.` |
   | 3 | `profile.filesystem_type not in _SUPPORTED_PHYSICAL_FILESYSTEMS` | `The selected physical iPod uses an unsupported filesystem ({type or 'unknown'}). Stock iPods require a FAT-formatted Windows volume or an HFS-formatted Mac volume.` (substring `unsupported filesystem`) |
   | 4 | `not profile.safe_for_writes` | `_unsafe_profile_message(profile)` — see below |

   Virtual iPods skip checks 1–3 entirely (they may live below a host mount and
   the host filesystem may even be undetected — a synthesized virtual profile
   with `filesystem_type="virtual"` is accepted because
   `safe_for_writes` is re-evaluated after synthesis; note check 4 still applies).

7. Return `profile` (the *virtual-applied* profile object — tests assert identity
   `profile is expected` when the inspector was stubbed and no transformation
   applied).

### 7.3 `_unsafe_profile_message(profile)`

Assemble reasons in this order, then join with `"; "`:

1. if `profile.read_only` → `the volume is mounted read-only`
2. every entry of `profile.unsafe_write_reasons`
3. if `filesystem_type == ""` → `the actual filesystem type could not be detected`
4. if `not identity.is_complete` → `the mounted volume identity could not be verified`
5. if the list is still empty and `detection_errors` non-empty → append them

`detail = "; ".join(reasons) or "filesystem safety could not be verified"`.
Final message (one line; the two sentences are separated by a single space):

```
The iPod filesystem is not safe for writing: {detail}. Actual filesystem: {filesystem_type or 'unknown'}; device-reported format: {reported_volume_format or 'unknown'}.
```

### 7.4 `revalidate_device_write_readiness`

* **Virtual retained profile** (identity os == `"virtual"`), own code path:
  1. Re-inspect `retained.inspection_path or retained.mount_path` with the
     retained `reported_volume_format`, `probe_case_sensitivity=(probe is True)`;
     re-apply `virtual_ipod_profile`; `_log_unsafe_profile("revalidated", current)`.
  2. Still not virtual after synthesis →
     `DeviceWriteSafetyError("The virtual iPod metadata is no longer present at the selected path. podsync stopped before the next write.")`
  3. `current.identity != retained.identity` →
     `DeviceWriteSafetyError("The virtual iPod root changed after inspection. podsync stopped before the next write.")`
  4. `not current.safe_for_writes` →
     `DeviceWriteSafetyError(f"The virtual iPod is no longer safe to write: {_unsafe_profile_message(current)}")`
  5. Otherwise return `current`.
* **Physical retained profile:** `result = revalidate_filesystem_profile(...)`,
  `_log_unsafe_profile("revalidated", result.current_profile)` (silent when safe),
  and when `not result.safe_to_continue` raise
  `DeviceWriteSafetyError(f"The iPod volume is no longer safe to write: {result.reason} podsync stopped before the next write.")`
  — the reason text comes straight from §6.4 (e.g. contains
  `A different volume is mounted at the inspected path.` → substring
  `different volume`). Return `result.current_profile`.

### 7.5 `volume_lock_key(profile) -> str`

```
"|".join((identity.operating_system, identity.device_id,
          identity.volume_id, identity.mount_instance))
```

Example: `VolumeIdentity("linux", "8:17", "/dev/sdb1", "317")` →
`"linux|8:17|/dev/sdb1|317"`. This key is the scan-time identity handed to
`DeviceWriteGuard(volume_key=...)` (which then drops field 4 for locking, §2.2)
and to `expected_volume_identity_key` comparisons in §9/§10. It deliberately
contains stable volume facts, never a mount label or path.

### 7.6 Behavioral checklist

| Scenario | Result |
|---|---|
| Safe profile | returned unchanged; **no log records at WARNING/DEBUG** |
| `unsafe_write_reasons` non-empty | `DeviceWriteSafetyError` containing the reason text + `WARNING "Unsafe iPod filesystem profile inspected"` |
| Selected path is a subdir of the containing mount (non-virtual) | error containing `not mounted` |
| `filesystem_type="ntfs"` (non-virtual) | error containing `unsupported filesystem` |
| Virtual dir with `iPodInfo.json` below a host mount | accepted; `profile.mount_path == selected`; `identity.operating_system == "virtual"` |
| Virtual with incomplete host identity (`filesystem_type=""`, os=`macos` identity empty) | still accepted (virtual synthesis), and revalidation returns an **equal** identity |
| Revalidation `volume_changed` | error containing `different volume` |

---

## 8. `podsync.device.durability`

### 8.1 API

```python
def open_unique_sibling_temp(
    target: str | Path, *, mode: str = "w+b", encoding: str | None = None
) -> tuple[Path, IO[Any]]: ...
def flush_written_file(file: IO[Any], *, full: bool = False) -> None: ...
def flush_parent_directory(path: str | Path) -> None: ...
def durable_replace(source: str | Path, target: str | Path) -> None: ...
def durable_publish_new(source: str | Path, target: str | Path) -> bool: ...
def durable_unlink(path: str | Path, *, missing_ok: bool = False) -> None: ...
def flush_filesystem(
    mount_path: str | Path,
    *,
    allow_unavailable: bool = False,
    require_volume_barrier: bool = False,
) -> tuple[bool, str]: ...
```

### 8.2 Temp files and flushes

**`open_unique_sibling_temp`** — `tempfile.mkstemp(prefix=".iop-", suffix=".tmp",
dir=str(Path(target).parent))`. `mkstemp` provides `O_EXCL` semantics and an
already-open descriptor: there is **no check-then-open window**, and a stale or
malicious predictable `target + ".tmp"` symlink at that path is never touched.
If `fdopen` fails: close the descriptor, unlink the temp (both best-effort),
re-raise. Binary modes (`"b" in mode`) → `os.fdopen(fd, mode)`; text modes →
`os.fdopen(fd, mode, encoding=encoding or "utf-8")`. Caller owns **both** the
path and file object: flush + close before `durable_replace`, and remove the
path on failure. Compact prefix preserves room on short-component filesystems.

**`flush_written_file(file, *, full=False)`** — always `file.flush()` then
`os.fsync(fd)`; then:

| platform | additional barrier |
|---|---|
| `win32` | `kernel32.FlushFileBuffers(msvcrt.get_osfhandle(fd))`; failure raises `OSError(code, FormatError(code).strip() or "FlushFileBuffers failed")` |
| `darwin` **and** `full=True` | `fcntl.fcntl(fd, getattr(fcntl, "F_FULLFSYNC", 51))`; if `fcntl.fcntl` is not callable → `OSError("macOS fcntl() is unavailable")` |
| else | nothing |

**`flush_parent_directory(path)`** — **no-op on `win32`** (Windows sessions get
their volume barrier from `flush_filesystem` + safe eject). POSIX: open
`Path(path).parent` with `O_RDONLY | O_DIRECTORY` (if defined), `fsync`,
`close` in `finally`. (Test contract: exactly `open(parent_str)` → `fsync(fd)` →
`close(fd)`.)

### 8.3 Atomic operations and their ordering

| Function | Exact sequence | On failure |
|---|---|---|
| `durable_replace(src, dst)` | 1) `os.replace(src, dst)` 2) `flush_parent_directory(dst)` | propagates; events observed by tests must be exactly `replace` then `directory` |
| `durable_unlink(path, missing_ok=False)` | 1) `Path(path).unlink()` 2) `flush_parent_directory(path)` | `FileNotFoundError` → return if `missing_ok` else re-raise; **no retry loop exists — a single unlink attempt, errors propagate** (except as noted below) |
| `durable_publish_new(src, dst) -> bool` | see below | see below |

**`durable_publish_new`** publishes a flushed file **without clobbering** an
existing target (manifest/blob publication):

1. Try `os.link(src, dst)`.
   * `FileExistsError` → **re-raise immediately**; target bytes and source both
     preserved (no-clobber contract).
   * Other `OSError` (FAT/exFAT/network filesystems lack hard links) → fallback:
     `os.open(dst, O_WRONLY|O_CREAT|O_EXCL, 0o600)`, then copy
     `src` → `dst` via `shutil.copyfileobj` inside `with open(src,"rb")`,
     `flush_written_file(target_file)`. Copy/flush failure → close a still-open
     descriptor, best-effort `durable_unlink(dst, missing_ok=True)`
     (swallow `OSError`), re-raise.
2. `flush_parent_directory(dst)`:
   * success → continue;
   * `OSError` → best-effort `durable_unlink(dst, missing_ok=True)` so callers
     never see an unconfirmed publication (unlink failure →
     `logger.exception("Could not remove unconfirmed publication target %s", dst)`)
     then **re-raise the flush error**.
3. `durable_unlink(src)`:
   * success → return `True`;
   * `OSError` → `logger.warning("Published %s, but its temporary source %s could not be removed: %s", ...)` and return **`False`** — cleanup failure alone never
     turns a committed publication into an error.

Return contract: `True` = published and temp removed; `False` = published but
temp left behind; exceptions = not durably published (target removed where
possible).

Because every temp is a **sibling** of its target (same directory ⇒ same
filesystem), `os.replace`/rename inside this module never crosses devices;
cross-device inputs given by a caller would surface the raw `OSError` from
`os.replace` (documented edge case — callers must stage in the target's
directory, which `metadata_write` and the database writer do).

### 8.4 `flush_filesystem(mount_path, *, allow_unavailable=False, require_volume_barrier=False) -> (bool, str)`

Shared anchor: `_committed_database_path(mount)` = `info.resolve_itdb_path(str(mount))`
(Chapter 06 authority) and the candidate must be a regular file with
`st_size > 0`; otherwise `None` (`OSError` during the probe → `None`). A
zero-byte alternate-name marker is therefore never used as an anchor.

**Linux branch** (`sys.platform.startswith("linux")`):

| situation | result |
|---|---|
| `shutil.which("sync")` is falsy | `(False, "sync utility unavailable")`; with `allow_unavailable` → `(True, "sync utility unavailable; relying on the unmount flush")` |
| `subprocess.run(["sync", "-f", str(mount)], timeout=15)` raises `FileNotFoundError` | same as missing `sync` |
| `TimeoutExpired` | `(False, "filesystem flush timed out")` — **never** converted to success by `allow_unavailable` |
| exit ≠ 0 | `(False, stderr.strip() or stdout.strip() or f"filesystem flush failed with code {rc}")` |
| exit 0 | `(True, output or "pending writes flushed")` |

**macOS branch:** first `os.sync()`; `AttributeError`/`OSError` →
`(False, f"macOS sync failed: {exc}")`, or with `allow_unavailable`
`(True, f"macOS sync unavailable ({exc}); relying on the unmount flush")`.
Then `_flush_database_anchor(full=True, ...)`:

* anchor found → open `"rb+"`, `flush_written_file(full=True)`; `OSError` →
  `(False, f"filesystem flush failed for {anchor}: {exc}")`; success →
  `(True, f"macOS full filesystem flush completed via {anchor}")`.
* anchor `None` → `(True, "macOS filesystem sync completed; no regular full-fsync anchor remains on the restored device")`
  (the `os.sync()` already ran; no directory `F_FULLFSYNC` is invented).

**Windows branch** → `_flush_database_anchor(full=False, ...)`:

* anchor `None` → `_windows_flush_volume_anchor(mount, allow_unavailable=...)`:
  resolve `GetVolumePathNameW(mount)` → root → `GetVolumeNameForVolumeMountPointW(root)`
  (fallback: `\\.\{drive}` from `os.path.splitdrive`); open
  `GENERIC_READ|GENERIC_WRITE`, share R/W, `OPEN_EXISTING`; then
  `FlushFileBuffers(handle)`.

  | failure stage | message (`allow_unavailable=True` turns all of these into `(True, msg + "; relying on the required safe-eject flush")`) |
  |---|---|
  | volume path unresolved | `could not resolve the iPod volume handle for a durability barrier` |
  | open failed | `could not open the iPod volume for a durability barrier: {FormatError or code}` |
  | flush failed | `Windows directory durability barrier failed: {FormatError or code}` |
  | success | `(True, f"Windows volume buffers flushed for {volume_path}")` |

* anchor found → open `"rb+"`, `flush_written_file(full=False)`; `OSError` →
  `(False, f"filesystem flush failed for {anchor}: {exc}")`. Then:
  * `require_volume_barrier=True` → run the volume anchor as well; failure →
    `(False, f"Windows file buffers flushed for {anchor}, but the full volume barrier failed: {volume_message}")`; success →
    `(True, f"Windows file buffers flushed for {anchor}; {volume_message}")`.
  * else → `(True, f"Windows file buffers flushed for {anchor}")`.

  (Key invariant: a file-handle flush alone is reported as *not* a complete
  volume barrier when `require_volume_barrier` demands one.)

**Other platforms:** `(False, f"filesystem flush is unsupported on {sys.platform}")`;
with `allow_unavailable` → `(True, "...; relying on the unmount flush")`.

`allow_unavailable` semantics summary: converts only *availability* problems
(no utility, no anchor, unsupported platform, macOS sync missing, volume-handle
resolution/open/flush unavailability on Windows) into `(True, "...relying on
the ... flush")`. Command timeouts and hard command failures stay `False`.

---

## 9. `podsync.device.metadata_write`

### 9.1 API

```python
@dataclass(slots=True)                      # mutable: profile is replaced on revalidate
class DeviceMetadataWriteSession:
    mount_path: Path
    filesystem_profile: FilesystemProfile

    def revalidate(self) -> FilesystemProfile: ...
    def write_bytes_atomic(
        self, relative_path: str | Path, data: bytes, *, allowed_subtree: str | Path
    ) -> Path: ...
    def write_text_atomic(
        self, relative_path: str | Path, text: str, *,
        allowed_subtree: str | Path, encoding: str = "utf-8"
    ) -> Path: ...
    def delete(
        self, relative_path: str | Path, *, allowed_subtree: str | Path,
        missing_ok: bool = False
    ) -> None: ...

@contextmanager
def guarded_device_metadata_session(
    mount_path: str | Path,
    *,
    reported_volume_format: str = "",
    expected_volume_identity_key: str = "",
) -> Iterator[DeviceMetadataWriteSession]: ...
```

### 9.2 `guarded_device_metadata_session` — arming order

1. `profile = inspect_device_write_readiness(mount_path, reported_volume_format=...)`
   (may raise; nothing has been locked or written yet).
2. `current_key = volume_lock_key(profile)`. If `expected_volume_identity_key` is
   non-empty **and** differs →
   `DeviceWriteSafetyError("A different volume is mounted at the selected iPod path. podsync stopped before writing device metadata.")`
   — raised **before** acquiring the guard (no lock attempt at all).
3. `with DeviceWriteGuard(mount_path, volume_key=current_key):` (default
   `track_database_generation=True`, `queue_in_process=True`).
4. Inside the guard:
   `profile = revalidate_device_write_readiness(profile, probe_case_sensitivity=True)`
   — this is where the case-sensitivity probe (§6.2) actually writes and removes
   its probe file, only after the exclusive lock is held.
5. `yield DeviceMetadataWriteSession(mount_path=Path(os.path.realpath(mount_path)),
   filesystem_profile=profile)`.
6. On exit the guard releases lock + queue slot (exceptions from the body
   propagate normally).

Typical subtrees: `"iPod_Control/Device"` (SysInfo, SysInfoExtended, HashInfo)
— the caller always passes `allowed_subtree` explicitly; there is no
default.

### 9.3 `write_bytes_atomic` — exact sequence

Given `relative_path`, `data`, `allowed_subtree`:

1. `payload = bytes(data)`.
2. `target = self._resolve(relative_path, ...)` → `resolve_device_path(mount_path, relative_path, allowed_subtree=...)` — traversal/absolute/symlink inputs raise
   `UnsafeDevicePathError` (a `ValueError`; test asserts `pytest.raises(ValueError)`
   for `"../outside"`).
3. `self._validate_component(target.name)`:
   `limit = int(self.filesystem_profile.max_component_length or 0)`; if
   `limit > 0 and len(name) > limit` →
   `DeviceWriteSafetyError(f"The metadata filename {name!r} exceeds this iPod filesystem's {limit}-character component limit.")`
   (long-path guard).
4. `require_file_size_supported(len(payload), max_file_size_bytes=self.filesystem_profile.max_file_size_bytes, display_name=target.name)`
   (§4; raises `FileSizeLimitError`).
5. `self._ensure_free_space(len(payload), target.name)`:
   * `shutil.disk_usage(self.mount_path).free`; `OSError` →
     `DeviceWriteSafetyError(f"Could not verify iPod free space before writing {display_name}: {exc}")`.
   * `required = allocated_size(logical, self.filesystem_profile.allocation_unit_size)`;
     `free < required` →
     `DeviceWriteSafetyError(f"The iPod does not have enough free space to safely write {display_name}. podsync stopped before creating the file.")`
     (substring `enough free space`). Free space is checked against the
     allocation-rounded size, not the logical size.
6. `self._ensure_parent(relative_path, ...)`: `revalidate()` → resolve → if
   `target.parent.is_dir()` return; else
   `target.parent.mkdir(parents=True, exist_ok=True)` then
   `flush_parent_directory(target.parent)`.
7. `self.revalidate()` (readiness revalidation #2), re-resolve `target`.
8. `tempfile.mkstemp(dir=str(target.parent), prefix=".iop-", suffix=".tmp")` —
   sibling temp in the same directory (same-filesystem rename guaranteed).
9. `with os.fdopen(fd, "wb") as file:` write payload; `flush_written_file(file)`
   (flush + `fsync`, plus platform barrier).
10. `self.revalidate()` (revalidation #3), re-resolve `target` again, then
    `durable_replace(temp_path, target)` — atomic `os.replace` + parent-directory
    fsync. Return `target`.
11. **Exception path:** close a still-open fd; `self._cleanup_temp_if_still_safe(temp_path)`
    (its own try/except: `revalidate()` then `durable_unlink(temp, missing_ok=True)`;
    any failure → `logger.warning("Could not safely remove temporary iPod metadata file %s: %s", ...)`, never raises), then re-raise the original error.

**Ordering contract** (asserted by tests): the last three observable events of a
successful write are `flush` → `revalidate` → `replace`. Full revalidation count
per successful write: after parent creation, after flush, plus one inside
`_ensure_parent` — i.e. **the volume identity is re-verified immediately before
every filesystem mutation** (mkdir, temp creation is preceded by a revalidate,
and the final replace).

`write_text_atomic` = `write_bytes_atomic(relative_path, text.encode(encoding), ...)`
(encoding keyword defaults `"utf-8"`).

`delete(relative_path, *, allowed_subtree, missing_ok=False)`:
`revalidate()` → `_resolve(...)` → `durable_unlink(target, missing_ok=missing_ok)`.
No size/free-space checks (deletion only needs containment + a live volume).

---

## 10. `podsync.device.eject`

### 10.1 API

```python
def eject_ipod(
    mount_path: str,
    *,
    reported_volume_format: str = "",
    expected_volume_identity_key: str = "",
) -> tuple[bool, str]: ...
```

Returns `(success, message)`; `message` is display-ready. Constants:
`_TIMEOUT_SECS = 30`, `_WINDOWS_VERIFY_SECS = 20`, `_WINDOWS_LOCK_RETRY_SECS = 10`;
mount-gone polls run at `0.25 s` intervals.

Benign-absence hint tuples (case-insensitive substring matching, used to treat
"already gone" as success):

* `_ALREADY_UNMOUNTED_HINTS = ("not mounted", "not currently mounted", "already unmounted")`
* `_MISSING_TARGET_HINTS = ("no such file or directory", "not found", "no object", "error looking up object", "does not exist")`

### 10.2 `eject_ipod` common prefix (exact order)

1. Empty/falsy `mount_path` → `(False, "No device path supplied.")`.
2. `path = Path(os.path.realpath(mount_path))`.
3. **Virtual short-circuit:** if `(path / "iPodInfo.json").is_file()` →
   `(True, "Virtual iPod closed; no operating-system eject was needed.")`
   — *before* any volume inspection or guard. A virtual iPod is an ordinary host
   directory; passing it to an OS eject API could unmount the host volume.
   (Tests stub `_inspect_eject_volume`/`_eject_windows` to fail if reached.)
4. `_inspect_eject_volume(path, reported_volume_format=...)`:
   * `inspect_filesystem_profile(path, ..., probe_case_sensitivity=False)`.
   * `normcase(realpath(selected)) != normcase(realpath(profile.mount_path))` →
     `DeviceWriteSafetyError("The selected iPod path is no longer mounted as its own volume. podsync stopped rather than ejecting the containing host volume.")`
     (contains `not mounted`).
   * `(path / "iPod_Control").is_dir()` must hold → else
     `DeviceWriteSafetyError("The selected volume no longer contains iPod_Control. podsync stopped rather than ejecting an unrecognized volume.")`
   * `profile.identity.is_complete` must hold → else
     `DeviceWriteSafetyError("The mounted volume identity could not be verified. Use the operating system's eject control for this iPod.")`
   * `DEBUG` log of mount/filesystem/reported/source/identity key.
5. `current_key = volume_lock_key(profile)`. If `expected_volume_identity_key`
   non-empty and ≠ `current_key` →
   `DeviceWriteSafetyError("A different volume is mounted at the selected iPod path. podsync stopped before ejecting it. Reconnect and reload the iPod.")`
   — raised **before** the guard (no eject attempt happens; test asserts
   `attempts == 0` and message contains `different volume`).
6. `with DeviceWriteGuard(path, volume_key=current_key, track_database_generation=False):`
   (test asserts both kwargs). Inside:
   `_revalidate_eject_volume(profile)`:
   * re-inspect at `retained.inspection_path or retained.mount_path` with the
     retained `reported_volume_format`, no case probe;
   * identity incomplete **or** `!= retained.identity` →
     `DeviceWriteSafetyError("The mounted volume changed while podsync was preparing to eject. Nothing was ejected; reconnect and reload the iPod.")`
   * `filesystem_type` differs → `DeviceWriteSafetyError("The filesystem at the selected iPod path changed while preparing to eject. Nothing was ejected.")`
   * normalized mount point differs → `DeviceWriteSafetyError("The iPod mount point changed while preparing to eject. Nothing was ejected.")`
7. Dispatch: `win32 → _eject_windows(path, read_only=...)`,
   `darwin → _eject_macos(path, read_only=...)`, else `_eject_linux(path, read_only=...)`
   where `read_only = current_profile.read_only` (captured *inside* the guard
   after revalidation).
8. Exception handling around the whole flow:
   * `DeviceWriteSafetyError` → `logger.warning("Safe eject refused for %s: %s", ...)` → `(False, str(exc))`
   * any other `Exception` → `logger.exception("eject_ipod: unexpected failure")` → `(False, f"Unexpected error: {exc}")`

Test contract: events observed are exactly `["lock", "eject", "unlock"]` — the
guard is held for the entire platform eject and released afterward, even on
failure (context manager).

### 10.3 Windows flow (`_eject_windows(path, *, read_only=False)`)

1. `drive = _windows_drive_from_path(path)` — regex `^([a-zA-Z]):` against the
   string or `path.drive`, uppercased (`"E:"`). Empty →
   `(False, f"Cannot determine drive letter from {path}.")`.
2. `not _windows_drive_is_mounted(drive)` → `(True, f"{drive} is already ejected.")`
   (non-Windows definition used only in tests: `Path("E:\\").exists()`; native:
   `GetDriveTypeW(root) != DRIVE_NO_ROOT_DIR (1)`).
3. **Flush gate first, before any privileged work** (unless `read_only`):
   `flush_filesystem(path)` — *strict* (no `allow_unavailable`). Failure →
   `(False, "podsync could not flush pending writes, so the iPod was not ejected. {flush_msg}")`
   and **zero** volume-lock/eject attempts happen (test asserts
   `privileged_attempts == 0`; message contains the original detail, e.g.
   `FlushFileBuffers failed`).
4. Capture the disk PnP id: PowerShell CIM script
   (`Win32_LogicalDisk` → `Win32_DiskPartition` → `Win32_DiskDrive.PNPDeviceID`),
   `powershell -NoProfile -NonInteractive -Command ...`, 30 s timeout,
   `CREATE_NO_WINDOW`; result protocol is a line `STATUS\tMESSAGE` with
   `STATUS ∈ {OK, MISSING, ERROR}` (`_parse_windows_eject_result` scans for the
   first such line). `OK` → the message is the PnP id; anything else →
   `pnp_device_id=None` and the failure text is kept as `pnp_msg`.
5. Strategy ladder — a step returns success only if it reported success **and**
   `_wait_for_windows_drive_removed` then confirms the drive letter is gone;
   otherwise the next step runs:

   | # | Strategy | Success wait |
   |---|---|---|
   | 1 | `_prepare_windows_volume_for_eject(drive)`: `CreateFileW("\\\\.\\E:", R/W, share R/W, OPEN_EXISTING)` → `FlushFileBuffers` → `FSCTL_LOCK_VOLUME (0x00090018)` **retried up to 10 s at 0.25 s intervals** → `FSCTL_DISMOUNT_VOLUME (0x00090020)` → `IOCTL_STORAGE_MEDIA_REMOVAL (0x002D4804)` with `prevent removal = 0` → `IOCTL_STORAGE_EJECT_MEDIA (0x002D4808)`; handle closed in `finally` | `_wait_for_windows_drive_removed` (20 s @ 0.25 s) |
   | 2 | `_run_windows_cfgmgr_eject(drive, pnp_id)`: PowerShell `Add-Type` P/Invoke `CM_Locate_DevNodeW` + `CM_Request_Device_EjectW`; veto or CM error → `ERROR` line with veto details | same wait |
   | 3 | `_run_windows_shell_eject(drive)`: PowerShell `Shell.Application` → `Namespace(17)` (This PC) → `ParseName(drive)` → `InvokeVerb("Eject")` | same wait |

   Distinct failure texts: open failure `Could not open {drive} for eject ({err}).`;
   flush failure `Could not flush pending writes for {drive} before eject ({err}).`;
   lock timeout `Could not lock {drive} for eject ({last_error}). Another process still has the iPod volume open.`;
   dismount refusal `Locked {drive}, but Windows refused to dismount it ({msg}).`;
   The two `IOCTL_STORAGE_*` results are ignored; prepare success message
   `Locked and dismounted {drive}.` If `kernel32` cannot be loaded at all the
   step raises `RuntimeError` (reaches the generic `Unexpected error:` handler).
   Returned success message of a step: its own message, or `Ejected {drive}`
   when empty.

   PowerShell runner (shared by the PnP lookup, CM eject and shell eject):
   missing binary → `PowerShell is not available on this system.`;
   timeout → `Eject timed out.`; status `OK`/`MISSING` **with exit 0** →
   success with the line's message (`MISSING` counts as success); status
   `ERROR` → failure with the line's message; no status line → failure
   `(stderr or stdout).strip()` or `"{default_error} PowerShell exited with code {rc}."`
   when exit ≠ 0, otherwise success with that text or `default_error`.
   Default errors: PnP lookup `Could not resolve the Windows disk device.`;
   CM eject `Windows device eject request failed.`; shell eject
   `Explorer eject request failed.` Values are embedded into the scripts as
   single-quoted PowerShell literals (`'` doubled). The CM script does its own
   CIM lookup when the PnP id is empty (`MISSING` if the logical disk is gone).
   CM script texts: locate failure
   `Windows could not locate the device node for {drive} (CM error {n}).`;
   veto `Windows vetoed eject for {drive} (CM error {r}, veto {t}[, {veto name}]).`;
   success `Windows accepted the eject request for {drive}.` Shell script:
   `Explorer did not expose the Computer namespace.` (ERROR),
   `{drive} is not mounted.` (MISSING),
   `Explorer accepted the eject request for {drive}.` (OK).
6. All strategies exhausted →
   `(False, "Windows did not eject {drive}; the drive is still mounted. Close any File Explorer windows or apps using the iPod, then try again." + "\n\nDetails: " + " | ".join(unique non-empty details))`
   — details in order `pnp_msg` (only when the PnP lookup failed), prepare,
   CM, shell; the `Details` suffix is omitted when there are none.

### 10.4 macOS flow (`_eject_macos(path, *, read_only=False)`)

1. `shutil.which("diskutil")` missing → `(False, "diskutil is not available.")`.
2. `info, info_msg = _macos_disk_info(mount)` (`diskutil info -plist`, timeout
   10 s; failures return `({}, reason)` rather than raising).
   Targets: `device_id = info["DeviceIdentifier"]`;
   `whole_disk = device_id if info["WholeDisk"] else info["ParentWholeDisk"]`;
   `disk_target = whole_disk or device_id or mount`;
   `volume_target = device_id or mount`;
   `mount_point = info["MountPoint"] or mount`.
3. `not info and not _macos_mount_is_present(mount_point, device_id)` →
   `(True, "Device already unmounted.")`.
4. **Flush gate** (unless `read_only`): `flush_filesystem(mount, allow_unavailable=True)`
   (strict on *failure* even though availability gaps are tolerated). Failure →
   `(False, "podsync could not flush pending writes, so the iPod was not ejected. {flush_msg}")`
   with **zero** `diskutil` eject/unmount attempts (test asserts
   `eject_attempts == []`; message contains e.g. `F_FULLFSYNC failed`).
5. Ladder (never forced — no `-force`, no `diskutil eraseDisk`, etc.):
   1. `["diskutil", "eject", disk_target]` → success + mount gone →
      `(True, f"Ejected {disk_target}")`.
   2. `["diskutil", "unmount", volume_target]`; if that succeeded, run
      `["diskutil", "eject", disk_target]`; accept if eject succeeded **or**
      its message is a benign absence, **and** mount gone →
      `(True, f"Unmounted and ejected {disk_target}")`.
   3. Final `if _wait_for_macos_mount_gone(...)` →
      `(True, f"Device unmounted: {disk_target}")`.
6. Otherwise `(False, f"diskutil could not safely eject {disk_target}; the iPod is still mounted. Close any files or apps using it, then retry." + (" Details: " + " | ".join(unique)) )`
   — details are `info_msg` followed by every attempt's message, deduplicated;
   the suffix is omitted when empty. Required substrings: `still mounted`,
   `Close`, `retry`, plus each attempt's detail (e.g. `in use`).

`_macos_disk_info` failure texts: `diskutil is not available.`,
`diskutil info timed out.`, stderr/stdout or `diskutil info failed.` on non-zero
exit, `Could not parse diskutil info: {exc}`,
`diskutil info returned an unexpected response.` (plist not a dict).

Mount presence `_macos_mount_is_present(mount_point, device_id)`:
run `mount` (30 s timeout); timeout → present (`True`); missing binary or
non-zero exit → fall back to `Path(mount_point).exists()`. Otherwise parse lines
with regex `^(.+) on (.+) \(.+\)$` (paths normalized via
`Path(p).resolve(strict=False)`); present iff normalized mount point matches or
the source equals `/dev/{device_id}`.
`_wait_for_macos_mount_gone`: 10 s deadline, 0.25 s poll, final check after
deadline.

### 10.5 Linux flow (`_eject_linux(path, *, read_only=False)`)

1. `device = _find_block_device(mount)`; `detach_target = parent(device) or device`
   (or `None`). Block-device discovery order:
   1. `findmnt -n -o TARGET,SOURCE --target <mount>` (5 s): accept only when
      `source.startswith("/dev/")`, target ≠ `/`, and the requested path equals or
      is beneath the target (normalized).
   2. `/proc/mounts` scan: longest non-root mount point containing the requested
      path whose device starts with `/dev/` (octal escapes decoded).
   3. `lsblk -J -o NAME,MOUNTPOINT` (5 s): any *child* entry whose mountpoint
      equals the path → `/dev/{name}`.
   4. else the best match from step 2 (`best`), else `None`.
2. **Mount-table verification first:** `_linux_path_is_mounted(mount, device)`;
   `OSError` (unreadable `/proc/mounts`) →
   `(False, "podsync could not verify the Linux mount table, so it did not attempt to eject the iPod: {exc}")`
   — **no flush, no unmount attempted** (required substrings `mount table`,
   `did not attempt`).
3. `not mounted` → `(True, "Device already unmounted.")`.
4. **Flush gate** (unless `read_only`): `_run_sync(mount)` →
   `flush_filesystem(mount, allow_unavailable=True)` (test asserts
   `allow_unavailable=True` is passed with the exact mount path). Failure →
   `(False, "podsync could not flush pending writes, so the iPod was not ejected. {flush_msg}")`
   with **zero** unmount/eject calls (message contains `flush` + e.g.
   `filesystem flush timed out`).
5. Ladder (again: **never forced** — no `-f`/`--force` flags ever):

   | # | Strategy | Details |
   |---|---|---|
   | 1 | `udisksctl` (requires `device` and the binary) | `udisksctl unmount --block-device {device} --no-user-interaction` (success text `udisksctl unmounted {device}.`, benign absence → OK with `Device already unmounted.`, else output or `udisksctl unmount failed.`) → `_wait_for_linux_mount_gone`, still mounted → failure `msg or "udisksctl did not unmount {device}."` → no parent device → `Unmounted {device}`; otherwise the step's result is `udisksctl power-off --block-device {parent} --no-user-interaction`: `Ejected {parent}`, benign absence → `Device already detached: {parent}`, else output or `udisksctl power-off failed.` Any failure (including a failed power-off after a good unmount) is recorded and falls through |
   | 2 | `eject` binary (requires `detach_target`) | `eject {parent-or-device}` (30 s; timeout `eject timed out.`, non-zero → output or `eject failed.`); success + gone → `Ejected {target}`; benign absence + gone → `Device already detached: {target}`; else record and fall through |
   | 3 | `umount` binary | targets, deduplicated in order: `device` (if known), then `mount` **only if `path.exists()`**. For each: exit 0 → `umount succeeded for {target}`; non-zero with benign absence → treated as `already_unmounted=True`; then wait for mount gone; on success optionally run a post-unmount detach (`udisksctl power-off` else `eject`, benign absence accepted) and return `(True, "{msg}; {detach_msg}")` or `(True, msg)` |
   | 4 | final wait | mount eventually gone → `(True, "Device already unmounted.")` |
   | 5 | exhausted | `(False, " | ".join(unique errors) or last_error or "No suitable unmount utility found (tried udisksctl, eject, and umount without forcing).")` |

   Command runner `_run_command(args, timeout=30)`: `FileNotFoundError` →
   `(False, f"{args[0]} is not available.")`; `TimeoutExpired` →
   `(False, f"{' '.join(args)} timed out.")`; exit 0 →
   `(True, output or f"{' '.join(args)} succeeded.")`; else
   `(False, output or f"{' '.join(args)} failed with code {rc}.")`
   where `output = (stderr or stdout).strip()`.
   `_run_umount_command` (own runner, 30 s) additionally returns the third
   `already_unmounted` flag: exit 0 → `umount succeeded for {target}`; timeout
   → `umount timed out.`; non-zero with benign absence → output or
   `Device already unmounted.` with the flag set; else output or `umount failed.`
   Post-unmount detach returns the power-off/eject message when it succeeded or
   was a benign absence, otherwise an empty string (the unmount still counts).
6. `_linux_path_is_mounted`: reads `/proc/mounts` via `_linux_mount_entries`
   (octal decode; **`OSError` → raise `OSError("Could not read the Linux mount table: {exc}")`** —
   this is what step 2 catches). Mounted iff: device equals the wanted device, or
   normalized mount point equals the wanted path, or the wanted path lies beneath
   a non-root mount point whose device starts with `/dev/` (subject to the wanted
   device when known).
7. `_wait_for_linux_mount_gone(mount, device)`: 10 s deadline, 0.25 s poll; any
   `OSError` from the mount-table read during polling → warn + return `False`
   (fail closed).

**Parent-device derivation** (`_parent_block_device("/dev/sdb1") → "/dev/sdb"`):
name patterns in order — `^(nvme\d+n\d+)p\d+$` → strip `pN`;
`^(mmcblk\d+)p\d+$` → strip `pN`; `^([a-z]+)\d+$` → strip trailing digits;
otherwise `None`.

### 10.6 Eject edge cases (explicit)

| Case | Behavior |
|---|---|
| Read-only mount | Allowed: guard + revalidation still run; **flush step skipped** on all three OSes (`read_only=True`); Windows prepare still runs (lock/dismount need no writes beyond `FlushFileBuffers` on the volume handle) |
| Missing/absent device | Windows: `already ejected` success; macOS: `Device already unmounted.` success; Linux: `Device already unmounted.` success |
| Volume replaced between selection and guard | `different volume` refusal before lock (expected-key mismatch) or inside guard (`_revalidate_eject_volume`) |
| Interrupted/failed eject | guard always released by `with`; partial progress reported in the failure `Details:` join; **no forced retry is ever attempted** |
| Busy volume | macOS/Linux leave it mounted and report failure with "close … retry" guidance; Windows reports veto details from CM/shell |
| Virtual iPod | immediate `(True, …)` before any OS API |

---

## 11. `podsync.device.recovery`

### 11.1 API

```python
@dataclass(frozen=True, slots=True)
class LinuxMountDetails:
    mount_point: str
    source: str
    filesystem: str
    options: tuple[str, ...]
    super_options: tuple[str, ...]
    @property
    def is_read_only(self) -> bool: ...   # "ro" in options or "ro" in super_options
    @property
    def summary(self) -> str: ...

@dataclass(frozen=True, slots=True)
class LinuxFilesystemRecoveryPlan:
    mount_path: str
    source: str
    filesystem: str
    kind: Literal["fat", "exfat", "mac", "ntfs", "unknown"]
    unmount_command: str
    identify_command: str
    checker_command: str

def linux_mount_details(path: str | Path) -> LinuxMountDetails | None: ...
def linux_filesystem_recovery_plan(
    mount_path: str | Path, *, filesystem: str = "", source: str = ""
) -> LinuxFilesystemRecoveryPlan: ...
```

`summary` format:
`f"{source or 'unknown device'} on {mount_point} ({filesystem or 'unknown filesystem'}, {','.join(options)})"`.

### 11.2 `linux_mount_details(path)`

* Non-Linux platform (`sys.platform` not starting with `linux`) → `None`.
* `target = os.path.realpath(path)`; parse `/proc/self/mountinfo` lines
  (same grammar as §6.3: split, require `"-"` separator, pre-separator ≥ 6
  fields, post-separator ≥ 3 fields; lines missing the separator are skipped).
  Fields: `mount_point = realpath(decode(fields[4]))`,
  `options = tuple(fields[5].split(","))`,
  `filesystem = fs_fields[0]`, `source = decode(fs_fields[1])`,
  `super_options = tuple(fs_fields[2].split(","))`.
* Skip root mounts (`mount_point == "/"`); match `target == mount_point` or
  `target.startswith(mount_point + os.sep)`; **longest mount point wins**
  (compared by `len(mount_point)` against the previous best, including its
  separators as stored — replicate the `rstrip(os.sep) or os.sep` normalization
  used for matching and the raw value for length).
* No match → `None`. Read/parse failure of `/proc/self/mountinfo` → empty list →
  `None` (never raises).

### 11.3 `linux_filesystem_recovery_plan(mount_path, *, filesystem="", source="")`

Read-only **string** construction; nothing is executed.

1. `details = linux_mount_details(str(mount_path))`;
   `actual_mount = details.mount_point if details else str(mount_path)`;
   `actual_filesystem = (filesystem or details.filesystem or "").strip().casefold()`;
   `actual_source = (source or details.source or "").strip()`.
2. Kind classification:

   | condition (on `actual_filesystem`) | `kind` | `checker_command` |
   |---|---|---|
   | in `{fat, fat16, fat32, msdos, msdosfs, vfat}` | `"fat"` | `f"sudo fsck.fat -n {shlex.quote(actual_source)}"` or `""` if no source |
   | `== "exfat"` | `"exfat"` | `f"sudo fsck.exfat -n {shlex.quote(actual_source)}"` or `""` |
   | `filesystem_itunesdb_platform(fs) == 1` (apfs/hfs family, §5.2) | `"mac"` | `""` |
   | `== "ntfs"` | `"ntfs"` | `""` |
   | anything else | `"unknown"` | `""` |

3. Commands (always present, `shlex.quote(actual_mount)` = `quoted_mount`):
   * `unmount_command = f"sudo umount {quoted_mount}"`
   * `identify_command = f"findmnt -no SOURCE,FSTYPE,OPTIONS --target {quoted_mount}"`

Example: mount `/media/user/IPOD` on `/dev/sdz1` vfat (ro) →
`unmount_command == "sudo umount /media/user/IPOD"`,
`checker_command == "sudo fsck.fat -n /dev/sdz1"`, `kind == "fat"`;
hfsplus → `kind == "mac"`, empty checker.

---

## 12. `podsync.device.dump`

Read-only diagnostics. **Never writes to the device.**

### 12.1 Constants and helpers

```python
IDENTITY_FIELDS: tuple[str, ...] = (
    "serial", "firewire_guid", "model_number", "model_family", "generation",
    "capacity", "color", "firmware", "board", "family_id", "updater_family_id",
    "product_type", "usb_vid", "usb_pid", "usb_serial", "scsi_vendor",
    "scsi_product", "scsi_revision", "connected_bus", "reported_volume_format",
    "filesystem_type", "db_version", "shadow_db_version", "uses_sqlite_db",
    "supports_sparse_artwork", "max_tracks", "max_file_size_gb",
    "max_transfer_speed", "podcasts_supported", "voice_memos_supported",
)

def dump_device_info(
    mount_path: str, *, include_raw: bool = False, probe_usb_vendor: bool = True
) -> dict[str, Any]: ...
def main(argv: list[str] | None = None) -> int: ...
```

Internal helpers (specify behavior, they are private):

| Helper | Behavior |
|---|---|
| `_device_dir(mount)` | `Path(mount) / "iPod_Control" / "Device"` |
| `_mount_name(mount)` | `win32`: drive from `os.path.splitdrive`, else `f"{first_alpha.upper()}:"` if the path starts with a letter; else `os.path.basename(os.path.normpath(mount)) or mount` |
| `_normalise_mount_path(mount)` | `win32`: strip whitespace and surrounding `"`; a bare 2-char `"D:"` becomes `"D:\"`; other platforms unchanged |
| `_sha256(bytes)` | hex digest |
| `_json_safe(value, *, include_raw=False)` | `bytes` → `{"bytes": len, "sha256": hex}` (+ `"text"` = UTF-8 `errors="replace"` decode when `include_raw`); `dict` → keys sorted by `str(k)`, values recursed; `list/tuple` → list, recursed; else the value as-is |
| `_normalise_for_compare(field, value)` | `None`/`""` → `""`; `firewire_guid` → `sysinfo.normalize_guid` (best-effort); `usb_pid` → `f"0x{int(value):04X}"` (fallback: stripped upper string); else `str(value).strip()` |
| `_append_identity_evidence` / `_append_plain_evidence` | for each field in `IDENTITY_FIELDS` with non-`None`/non-`""` value, append `{"source": sources.get(field, default_source), "value": value}` to `evidence[field]` (`sources` = `data.get("_sources", {})`) |
| `_rejected_conflicts(final, evidence)` | fields sorted; skip fields whose *final* normalized value is empty; any evidence entry whose normalized value is non-empty **and differs** from the final → `{"field", "final_value", "rejected_value", "rejected_source"}` |

### 12.2 `dump_device_info` — report shape

1. `mount_path = os.path.abspath(_normalise_mount_path(mount_path))`.
2. `snapshot = _final_identity_snapshot(mount)`:
   from **Chapter 06** `podsync.device.scanner` call
   `_probe_hardware(mount, mount_name)`, `_probe_filesystem(mount)`, and
   `_resolve_model(hardware, filesystem, _disk_size_gb(mount))`;
   `_disk_size_gb` = `round(shutil.disk_usage(mount).total / 1e9, 1)` on success
   else `0.0`. Report keys: `{"hardware", "filesystem", "resolved"}`.
3. Sections gathered:
   * `sysinfo`: missing file → `{"present": False}`; else
     `{"present": True, "path", "sha256"` (of the UTF-8 `errors="replace"`
     encoding of the text)`, "fields": parse_sysinfo_text(raw), "identity":
     identity_from_sysinfo(parsed, "sysinfo")}`.
   * `disk_sysinfo_extended`: missing → `{"present": False}`; else
     `{"present": True, "path", "bytes": len(raw), "sha256", "used_regex_fallback",
     "keys": sorted(plist), "identity", "cover_art_formats", "photo_formats",
     "chapter_image_formats"}`; **plus** `"raw"` (raw `bytes`) and `"plist"` when
     `include_raw`.
   * `live_scsi_vpd`: per platform — `win32` → `vpd_windows.query_ipod_vpd_for_path(mount, usb_pid=…, serial_filter=firewire_guid)`;
     `linux` → `vpd_linux.query_ipod_vpd_for_path(...)`; `darwin` →
     `vpd_iokit.query_ipod_vpd(...)`; other → `{"available": False, "reason": f"unsupported_{sys.platform}"}`.
     Empty result → `{"available": True, "result": None, "error": "no_result"}`;
     exception → `{"available": True, "result": None, "error": repr(exc)}`.
     Non-empty → `{"available": True, "result", "identity": _identity_from_live_vpd(result, source), "standard_inquiry": {"vendor", "product", "revision"}}`
     where `source = result.get("_source")` or the platform default
     (`windows_scsi`/`linux_scsi`/`scsi_vpd`), and `_identity_from_live_vpd`
     parses `vpd_raw_xml` (or wraps the dict), then overlays `usb_pid`,
     `usb_vid`, `usb_serial`, `scsi_vendor`, `scsi_product`, `scsi_revision`,
     `block_device` (when not `None`/`""`/`b""`) with `_sources[field] = source`.
   * `live_windows_scsi_vpd`: the same object on `win32`, else
     `{"available": False, "reason": "not_windows"}`.
   * `live_usb_vendor`: when `probe_usb_vendor` →
     `vpd_usb_control.query_ipod_usb_sysinfo_extended(usb_pid=…, serial_filter=firewire_guid)`.
     Empty result → `{"available": True, "result": None, "error": "no_result", "backend": backend_diagnostic()}`;
     non-empty → `{"available": True, "result", "identity": _identity_from_live_vpd(result, source), "backend"}`
     with `source = result.get("_source") or "usb_vendor"` (no
     `standard_inquiry` here); any exception →
     `{"available": False, "result": None, "error": repr(exc)}`; disabled →
     `{"available": False, "reason": "disabled"}`.
   * `standard_inquiry` (top level) = `live_scsi_vpd.get("standard_inquiry", {})`.
   * `usb_details`: subset of hardware keys
     `{usb_vid, usb_pid, firewire_guid, usbstor_instance_id, usb_parent_instance_id, usb_grandparent_instance_id}`
     present with non-`None`/non-`""` values.
   * Evidence collection in this order: `"sysinfo"`, `"sysinfo_extended"`,
     `"hardware"` (plain), `"live_scsi"`, `"usb_vendor"` →
     `all_identity_evidence`.
   * `final_resolved_identity` = `snapshot["resolved"]`;
     `resolver_conflicts` = `final_identity.get("_conflicts", [])`;
     `rejected_conflicting_evidence` per §12.1.
4. Top-level keys, in order:
   `mount_path, mount_name, sysinfo, disk_sysinfo_extended, live_scsi_vpd,
   live_windows_scsi_vpd, live_usb_vendor, standard_inquiry, usb_details,
   final_resolved_identity, resolver_conflicts, all_identity_evidence,
   rejected_conflicting_evidence`.
5. Return `_json_safe(report, include_raw=include_raw)` (guarantees the dict is
   `json.dumps`-serializable).

### 12.3 CLI (`python -m podsync.device.dump`)

`argparse`: positional `paths` (nargs `*`), `--all`, `--include-raw`,
`--no-usb-vendor`. Behavior:

1. `paths = list(args.paths)`; if `--all` **or** no paths were given, extend with
   the default discovery from **Chapter 06** `podsync.device.scanner._find_ipod_volumes()`
   (each `(mount, _display)` pair contributes `mount`), de-duplicating.
2. Still empty → `print("No iPod volumes found.", file=sys.stderr)`; return `1`.
3. Otherwise build `dump_device_info` per path with the flag mapping
   (`include_raw=args.include_raw`, `probe_usb_vendor=not args.no_usb_vendor`),
   print `json.dumps(payload, indent=2, ensure_ascii=False)` where `payload` is
   the single report for one path or a **list** for several; return `0`.
4. Module guard: `if __name__ == "__main__": raise SystemExit(main())`.

---

## 13. `podsync.device.linux_integration`

### 13.1 API

```python
RULE_VERSION = "2"
RULE_FILENAME = "61-podsync.rules"
RULE_DESTINATION = f"/etc/udev/rules.d/{RULE_FILENAME}"

class LinuxIntegrationState(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    READY = "ready"
    SETUP_REQUIRED = "setup_required"
    REFRESH_REQUIRED = "refresh_required"
    RULE_OUTDATED = "rule_outdated"

@dataclass(frozen=True)
class LinuxIdentityIntegration:
    state: LinuxIntegrationState
    explanation: str
    setup_instructions: str = ""
    @property
    def needs_setup(self) -> bool: ...   # state not in {NOT_APPLICABLE, READY}

def udev_rule_text() -> str: ...
def describe_linux_identity_integration(
    mount_path: str, *, product_serial: str = "", platform: str | None = None
) -> LinuxIdentityIntegration: ...
def linux_identity_setup_needed(
    device: object | None, *, platform: str | None = None
) -> bool: ...

__all__ = [
    "LinuxIdentityIntegration", "LinuxIntegrationState",
    "RULE_DESTINATION", "RULE_FILENAME", "RULE_VERSION",
    "describe_linux_identity_integration", "linux_identity_setup_needed",
    "udev_rule_text",
]
```

Host search order for an installed rule (`_HOST_RULE_PATHS`, first **readable**
entry wins — mirrors udev's highest-priority-directory-wins behavior, so a stale
`/etc` copy is never masked by a matching vendor copy later in the list):

1. `/etc/udev/rules.d/61-podsync.rules`
2. `/run/udev/rules.d/61-podsync.rules`
3. `/usr/local/lib/udev/rules.d/61-podsync.rules`
4. `/usr/lib/udev/rules.d/61-podsync.rules`
5. `/lib/udev/rules.d/61-podsync.rules`

### 13.2 Functions

* **`udev_rule_text()`** —
  `importlib.resources.files("podsync").joinpath("assets", "linux", RULE_FILENAME).read_text(encoding="utf-8")`.
  The bundled rule must publish `ID_PODSYNC_PRODUCT_SERIAL` and
  `ID_PODSYNC_RULE_VERSION={RULE_VERSION}` properties (the setup script below
  verifies both). Any `FileNotFoundError`/read error propagates unchanged.
  **Bundled rule contract** (the file ships in the package; four rules, all
  scoped to `ACTION` add|change, `SUBSYSTEM` block, `ENV{DEVTYPE}` disk):
  1. clear both `ID_PODSYNC_RULE_VERSION` and `ID_PODSYNC_PRODUCT_SERIAL` for
     every disk (so a stale value never survives a change event);
  2. for disks whose parent attributes are vendor `Apple*` and model `iPod*`,
     set `ID_PODSYNC_RULE_VERSION` to `2` **before** probing, so a failed probe
     is distinguishable from a rule that never ran;
  3. for the same disks run the udev helper `scsi_id --page=0x80 --whitelisted --device=/dev/%k`
     (non-absolute name — udev resolves helpers from its own directory; the
     legacy `--whitelisted` spelling keeps old udev/eudev working); when the
     result matches `SApple*iPod*`, set `ID_PODSYNC_PRODUCT_SERIAL` to the
     third whitespace-separated field of the result (the page 0x80 product
     serial);
  4. only when that serial is non-empty, add the symlink
     `disk/by-id/ipod-{serial}` with `string_escape=replace`.
  The rule changes no permissions and leaves the standard `ID_SERIAL` values
  (which carry the FireWire GUID) untouched. File comments explain the above.
* **`_installed_rule_state() -> tuple[bool, bool]`** —
  `(installed, matches_canonical)`; iterate `_HOST_RULE_PATHS` in order, take the
  first path that *reads* successfully (`OSError` → continue, including
  `/dev/null` symlinks failing or unreadable files), compare its text to
  `udev_rule_text()`; no readable candidate → `(False, False)`.
* **`describe_linux_identity_integration(...)`** — `active_platform =
  sys.platform if platform is None else platform`. Decision table:

  | condition (in order) | state | explanation |
  |---|---|---|
  | not `active_platform.startswith("linux")` | `NOT_APPLICABLE` | `Linux host integration is not applicable on this platform.` (no instructions) |
  | `product_serial.strip()` non-empty | `READY` | `The Apple product serial is already available.` (no instructions) |
  | not installed | `SETUP_REQUIRED` | `The podsync Linux identity rule is not installed on this host.` |
  | installed but content ≠ canonical | `RULE_OUTDATED` | `The highest-priority podsync Linux identity rule is disabled or outdated.` |
  | installed & current | `REFRESH_REQUIRED` | `The identity rule is installed, but this iPod has not published its Apple product serial. Reinstalling and triggering only this block device will retry without disconnecting it.` |

  For the last three states `setup_instructions = _setup_instructions(mount_path)`
  (the full shell script below). `needs_setup` is `True` exactly for
  `SETUP_REQUIRED`, `REFRESH_REQUIRED`, `RULE_OUTDATED`.
* **`linux_identity_setup_needed(device, *, platform=None) -> bool`**:
  * `active_platform` as above; `not startswith("linux")` **or** `device is None` → `False`.
  * `str(getattr(device, "serial", "") or "").strip()` non-empty → `False`.
  * Otherwise `True` iff **any** of:
    - `str(getattr(device, "path", "") or "").strip()` non-empty;
    - `int(getattr(device, "usb_pid", 0) or 0)` (TypeError/ValueError → 0) is in
      `podsync.device.models.IPOD_USB_PIDS` (Chapter 06 constant);
    - `str(getattr(device, "firewire_guid", "") or "").strip()` fullmatches
      `r"(?:0x)?[0-9A-Fa-f]{16}"`.

### 13.3 `_setup_instructions(mount_path)` — script contract

Returns a single POSIX shell script (stdout/stderr text for the *user* to run;
the application never executes it). `mount_path` is embedded `shlex.quote`d as
`MOUNT=...`; the canonical rule text is embedded in a `<<'PODSYNC_UDEV_RULE'`
heredoc (quoted delimiter → no expansion; the rule text is right-stripped
first). The whole script is one `( set -eu … )` subshell so it can be pasted
into an interactive shell without `exit` closing it. Required behaviors, in
order:

1. Set `RULE_DEST=/etc/udev/rules.d/61-podsync.rules`,
   `RULE_EXPECTED_VERSION=2`; prepend `/usr/sbin:/usr/bin:/sbin:/bin` to `PATH`
   while preserving existing entries; `log()` prefixes `podsync: `,
   `die()` prefixes `podsync Linux identity setup failed: ` and exits 1.
2. **Container refusal** (exit before touching anything) when any of:
   `/.dockerenv` exists, `/run/.containerenv` exists, `$container` non-empty,
   `$DISTROBOX_ENTER_PATH` non-empty, `$FLATPAK_ID` non-empty → die with
   instructions to run on the host, not in Distrobox/Toolbox/Flatpak/containers.
3. Require commands: `findmnt sed readlink lsblk id mkdir mktemp cp chmod rm mv udevadm grep`;
   `$MOUNT` must be an existing directory.
4. Resolve `PART="$(findmnt -n -o SOURCE --target "$MOUNT")"` (strip `\[...$`),
   `readlink -f` it, require a `/dev/` **block** device (`[ -b ]`); derive the
   whole disk via `lsblk -ndo PKNAME` (parent `/dev/$PKNAME` when non-empty,
   else the partition itself); the sysname must fullmatch `[a-zA-Z0-9._-]+`
   and exist under `/sys/class/block/`.
5. **Identity gate:** sysfs `device/vendor` must start with `Apple` and
   `device/model` must start with `iPod`, else die (never writes a rule for a
   non-iPod disk).
6. Refuse if `$RULE_DEST` is a directory; `mkdir -p -m 0755 /etc/udev/rules.d`
   as root (sudo/doas/root detection via `id -u`, `sudo`, `doas`).
7. **Atomic install:** write the rule to a `mktemp` file
   `${TMPDIR:-/tmp}/podsync-udev.XXXXXX`; copy into
   `/etc/udev/rules.d/.61-podsync.rules.new.$$`, `chmod 0644`, re-check the
   destination isn't a directory, `mv -f` into place (atomic rename that
   inherits the /etc security context — important on SELinux); traps
   (`EXIT`/`HUP`/`INT`/`TERM`) clean both staging paths.
8. `udevadm control --reload-rules`; then retrigger **only this disk**:
   if `udevadm trigger --help` mentions `settle` (systemd-udev ≥ 238) use
   `udevadm trigger --action=change --subsystem-match=block --sysname-match="$SYSNAME" --settle`,
   else the same trigger without `--settle` followed by
   `udevadm settle -t 15 || true` (bounded — unrelated device activity must not
   hang setup).
9. Verify: read `udevadm info --query=property --name="$DISK"`; extract
   `ID_PODSYNC_PRODUCT_SERIAL=` and `ID_PODSYNC_RULE_VERSION=`. Serial present →
   print `ID_PODSYNC_PRODUCT_SERIAL={serial}` to stdout, exit 0.
10. On failure emit diagnostics (disk, sysfs path, vendor/model, udevadm version,
    expected vs observed rule version with three-way interpretation: version
    matched but scsi_id produced nothing / different version copy taking
    precedence / marker missing), probe `scsi_id --page=0x80 --whitelisted --device=$DISK`
    from PATH or `/usr/lib/udev/scsi_id` / `/lib/udev/scsi_id`, run
    `udevadm test --action=change "$SYSPATH"` filtered through
    `grep -Ei '61-podsync|podsync|scsi_id|apple|ipod|error|failed|invalid|unknown'`,
    then die with "rule installed, but the Apple product serial was not
    published; diagnostics are above".

---

## 14. Cross-cutting edge-case matrix

| Edge case | Where handled | Exact behavior |
|---|---|---|
| Read-only mount | `filesystem_profile` (`ro` option / `Writable is False` / `FILE_READ_ONLY_VOLUME`), `write_readiness`, `eject` | `safe_for_writes=False` → readiness raises (`the volume is mounted read-only`); case probe skipped; eject proceeds but **skips flush** |
| Missing device / vanished path | `capture_database_generation`, `revalidate_filesystem_profile`, eject ladders | DB absent → `exists=False` generation (never equal to a present one); identity read failure → `identity_unavailable` refusal; eject reports "already ejected/unmounted" success |
| Partial write / crash mid-write | `metadata_write`, `durability` | payload is fully written + `fsync`ed to a sibling temp *before* `os.replace`; a crash leaves at most an orphan `.iop-*.tmp` — the target keeps its old bytes; the `except` path best-effort deletes the temp after revalidation |
| Interrupted eject | `eject` | context manager always releases guard; failed strategies fall through to a joined failure message; **never** escalates to a forced operation |
| Cross-device rename | `durability`, `metadata_write` | temps are always created in the target's directory, so `os.replace` is same-filesystem by construction; a caller violating this gets the raw `OSError` |
| Long paths / short component limits | `metadata_write._validate_component`, `filesystem_profile` | `max_component_length` from `PC_NAME_MAX`/`GetVolumeInformationW`; final filename longer than the limit → `DeviceWriteSafetyError`; `.iop-` temp prefix stays compact to fit |
| Reserved/unsafe names | `path_safety` | empty, `.`, `..`, colon-containing components, absolute forms, drive letters, UNC, NUL — all rejected before any syscall touches them |
| Symlink/reparse at any layer | `path_safety`, `write_guard` (lock dir/lock file), `durability` (unique temp) | component `lstat` walk rejects links; lock opens with `O_NOFOLLOW`/`FILE_FLAG_OPEN_REPARSE_POINT`; temps use `mkstemp` so predictable-name symlinks are irrelevant |
| Stale zero-byte alternate DB filename | via `info.resolve_itdb_path` (Chapter 06) in `capture_database_generation` + `_committed_database_path` | never hashed as the live DB, never used as a flush anchor |
| Lock directory hostile state (symlink, other owner) | `write_guard` | `DeviceWriteSafetyError` before any lock file is opened |
| Same-process concurrent writers | `write_guard` queue | FIFO wait (default) or immediate `DeviceBusyError` (`queue_in_process=False`); same-thread nesting always `DeviceBusyError` |
| Mount alias / remount | `write_guard` key collapse + `volume_lock_key` | 4-field keys collapse to 3 fields for locking; remount changes `mount_instance` → revalidation fails `mount_changed` |
| Linux HFS `force` mounts | `filesystem_profile` | permanent `unsafe_write_reasons` entry → readiness raises; case probe never runs |
| Case-probe cleanup failure | `filesystem_profile` | `case_sensitive=None`, error string `Could not remove filesystem case probe: ...`, plus unsafe reason `Filesystem case sensitivity could not be verified` |
| Hard links unavailable (FAT/exFAT/NFS) | `durability.durable_publish_new` | `O_EXCL` copy fallback keeps the no-clobber contract |
| Sync/command unavailable vs failing | `durability`, `eject` | unavailable → allowed only when `allow_unavailable=True`; timeout/non-zero exit → always failure |

---

## 15. Scope exclusions

1. **No GUI/application imports.** None of the twelve modules may import
   `podsync.gui`, `podsync.application`, `podsync.podcasts`,
   `podsync.sqlitedb_writer`, or `podsync.sync.transcoder` (nor any `podsync.sync.*`
   module at all — the dependency direction is the reverse: `podsync.sync.*`
   imports `podsync.device.path_safety`). All twelve modules must import
   successfully in a headless environment with only the standard library plus
   the other `podsync` chapters' modules present. If an implementation feels the
   need for one of these excluded packages, that need does not exist in this
   specification — remove it.
2. **Deferred (lazy) imports are allowed only for device-local modules:**
   `eject`/`metadata_write` import their siblings normally; `write_guard` and
   `durability` import `podsync.device.info` lazily inside functions (to avoid
   import cycles); `dump` imports `podsync.device.sysinfo`,
   `podsync.device.scanner`, `podsync.device.vpd_*`, and
   `podsync.device.usb_backend` lazily inside functions. Nothing else may be
   pulled in at import time (no Qt, no plist GUI tooling, no transcoder).
3. **Consumers in other chapters** (listed so their contracts with this
   chapter are unambiguous):
   * `podsync.sync.ipod_track_paths` — consumes `resolve_device_path` /
     `UnsafeDevicePathError` per §3.5.
   * `podsync.itunesdb_writer.mhbd_writer` — consumes
     `inspect_device_write_readiness`, `require_file_size_supported`/
     `FileSizeLimitError`, `allocated_size` free-space preflight
     (`DeviceWriteSafetyError` containing `enough free space`), `durable_replace`,
     `detect_filesystem_type`, `resolve_itunesdb_platform`, and
     `DeviceWriteGuard.assert_database_unchanged` invoked immediately before the
     final replace.
   * `podsync.device.vpd_libusb.write_sysinfo` (chapter 06 §14.5) — consumes
     `guarded_device_metadata_session` (with `expected_volume_identity_key` +
     `reported_volume_format`).
   * `podsync.device.bootstrap` (chapter 06 §11) — consumes
     `inspect_device_write_readiness`, `revalidate_device_write_readiness`,
     `volume_lock_key`, `DeviceWriteGuard` and `flush_filesystem`.

---

## 16. Tests in this room that cover this chapter

| Test file | What it pins down | Spec section |
|---|---|---|
| `tests/test_write_guard.py` | lock file refusal, tracking-off short-circuit, mutual exclusion and FIFO queue, key collapse, external-change detection, refresh after own commit, readiness refusal messages | §2, §7 |
| `tests/test_filesystem_safety.py` | mount-table parsing, filesystem detection and platform selection logs, guarded database install, durability barriers | §5, §6, §8 |
| `tests/test_device_safety.py` | device-relative path rules, size ceilings and messages, guarded metadata sessions, Linux recovery plans | §3, §4, §9, §11 |
| `tests/test_eject_backends.py` | eject preconditions, flush ordering, per-OS backends and messages | §10 |
| `tests/test_track_paths.py` | unsafe track locations funnel through `resolve_device_path` | §3.5 |
| `tests/test_device_identity.py` (setup gate) | `linux_identity_setup_needed` | §13 |
