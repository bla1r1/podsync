"""Load, save, verify and tidy the iTunesDB of a mounted (or virtual) iPod.

``load_device_library`` only reads.  ``save_device_library`` serializes through
:func:`podsync.itdb.writer.write_itdb`, then re-reads the committed file and
checks every media reference before reporting success.
``clear_device_play_state`` removes the firmware's play-state files once a new
database has been committed.

Collaborators are reached through their modules at call time (``flatten.x``,
``hardware.y``) so callers and tests can substitute them.
"""

from __future__ import annotations

import logging
import os
import struct
from collections.abc import Callable
from pathlib import Path

from podsync.hardware.safety.durable import safe_unlink
from podsync.hardware.safety.guard import UnsafeWriteError
from podsync.hardware.safety.paths import PathEscapeError, safe_device_path
from podsync.itdb.writer.playlist import PlaylistRecord
from podsync.itdb.writer.track import TrackRecord

__all__ = [
    "ReadbackError",
    "clear_device_play_state",
    "load_device_library",
    "save_device_library",
    "verify_saved_library",
]

logger = logging.getLogger(__name__)

_ITUNES_DIR = "iPod_Control/iTunes"
# Files the firmware regenerates from its own state; stale copies must go after a commit.
_PLAY_STATE_FILES = ("Play Counts", "iTunesStats", "PlayCounts.plist", "OTGPlaylistInfo")
_HEADER_PROBE = 0x70
_HEADER_TZ_OFFSET = 0x6C
_MAX_REPORTED_PROBLEMS = 5


class ReadbackError(RuntimeError):
    """The database that was just written does not read back cleanly."""


# ── loading ─────────────────────────────────────────────────────────


def _nothing_loaded() -> dict:
    # Deliberately without "device_time_context": callers use its absence as "no database".
    return {
        "tracks": [],
        "dataset2_standard_playlists": [],
        "dataset3_podcast_playlists": [],
        "dataset5_smart_playlists": [],
        "playcounts_timezone_changed": False,
    }


def _header_utc_offset(db_path: Path) -> int | None:
    """The UTC offset iTunes stamped into the MHBD header, if the header is intact."""
    with open(db_path, "rb") as handle:
        head = handle.read(_HEADER_PROBE)
    if len(head) < _HEADER_PROBE or head[:4] != b"mhbd":
        return None
    return struct.unpack_from("<i", head, _HEADER_TZ_OFFSET)[0]


def _flatten_track_rows(rows: list[dict], flatten, to_format_text) -> None:
    for row in rows:
        children = row.pop("children", [])
        row.update(flatten.collect_strings(children))
        row.update(flatten.collect_track_extras(children))
        if "filetype" in row:
            row["filetype"] = to_format_text(row["filetype"])


def _flatten_playlist_rows(rows: list[dict], flatten) -> list[dict]:
    for row in rows:
        descriptors = row.pop("mhod_children", [])
        row.update(flatten.collect_strings(descriptors))
        row.update(flatten.collect_playlist_extras(descriptors))
        entries = []
        for wrapper in row.pop("mhip_children", []):
            if "data" not in wrapper:
                continue
            entry = wrapper["data"]
            entry.update(flatten.collect_entry_extras(entry.get("children", [])))
            entries.append(entry)
        row["items"] = entries
    return rows


def _merge_play_stats(ipod_path: Path, tracks: list[dict], clock, zone_moved: bool,
                      header_offset: int | None, include: bool, play_stats) -> None:
    stats = play_stats.read_play_stats(ipod_path / "iPod_Control" / "iTunes" / "Play Counts") if include else None
    if stats is None:
        for row in tracks:
            row.setdefault("recent_playcount", 0)
            row.setdefault("recent_skipcount", 0)
        return
    if zone_moved and any(entry.has_data for entry in stats):
        logger.warning(
            "The iPod's time zone changed since the database was written (header offset %s). "
            "Play Counts are merged in the current zone %s; plays from before the change may "
            "be shifted.", header_offset, clock.name,
        )
    play_stats.apply_play_stats(tracks, stats, time_context=clock)


def _add_onthego_rows(visible: list[dict], itunes_dir: Path, tracks: list[dict], onthego) -> None:
    """On-The-Go playlists join dataset 2 only, and never duplicate an existing id."""
    known = {int(row["playlist_id"]) for row in visible if row.get("playlist_id")}
    added = 0
    for row in onthego.load_onthego_playlists(str(itunes_dir), tracks):
        playlist_id = int(row.get("playlist_id") or 0)
        if playlist_id and playlist_id not in known:
            visible.append(row)
            known.add(playlist_id)
            added += 1
    if added:
        logger.info("Merged %d On-The-Go playlist(s) from the device", added)


def load_device_library(
    ipod_path: Path, *, raise_on_error: bool = False, include_playcounts: bool = True,
) -> dict:
    """Parse the device database into flat track and playlist rows.

    Never writes.  A missing database yields an empty result (or
    ``FileNotFoundError`` when *raise_on_error*); any other failure is logged and
    either re-raised or turned into the empty result.
    """
    import podsync.hardware as hardware
    import podsync.itdb.reader as reader
    import podsync.itdb.reader.cover_links as cover_links
    import podsync.itdb.reader.onthego as onthego
    import podsync.itdb.reader.play_stats as play_stats
    import podsync.itdb.spec.clock as clock_mod
    import podsync.itdb.spec.fields as fields
    import podsync.itdb.spec.flatten as flatten

    ipod_path = Path(ipod_path)
    located = hardware.locate_database(str(ipod_path))
    db_path = Path(located) if located else ipod_path / "iPod_Control" / "iTunes" / "iTunesDB"
    if not db_path.exists():
        if raise_on_error:
            raise FileNotFoundError(f"iTunesDB was not found at {db_path}")
        logger.info("No iTunesDB at %s; treating the library as empty", db_path)
        return _nothing_loaded()

    try:
        header_offset = _header_utc_offset(db_path)
        device_clock = clock_mod.load_device_clock(ipod_path, database_offset=header_offset)
        zone_moved = bool(clock_mod.zone_changed_since_write(device_clock, header_offset))
        # Timestamps in the file were written in the zone of the header, not today's zone.
        parse_clock = (
            clock_mod.DeviceClock.fixed_offset(header_offset)
            if zone_moved and header_offset is not None else device_clock
        )
        logger.debug("Reading %s (header offset %s, device zone %s)", db_path, header_offset, device_clock.name)

        datasets = flatten.split_datasets(reader.read_itdb(str(db_path), time_context=parse_clock))
        tracks = datasets.get("mhlt", [])
        _flatten_track_rows(tracks, flatten, fields.fourcc_text)
        cover_links.attach_cover_refs(tracks, db_path)
        _merge_play_stats(ipod_path, tracks, device_clock, zone_moved, header_offset,
                          include_playcounts, play_stats)

        visible = _flatten_playlist_rows(datasets.get("mhlp", []), flatten)
        podcast = _flatten_playlist_rows(datasets.get("mhlp_podcast", []), flatten)
        categories = _flatten_playlist_rows(datasets.get("mhlp_smart", []), flatten)
        _add_onthego_rows(visible, db_path.parent, tracks, onthego)

        logger.info(
            "Parsed iPod database: %d tracks, ds2_playlists=%d, ds3_playlists=%d, ds5_playlists=%d",
            len(tracks), len(visible), len(podcast), len(categories),
        )
        return {
            "tracks": tracks,
            "dataset2_standard_playlists": visible,
            "dataset3_podcast_playlists": podcast,
            "dataset5_smart_playlists": categories,
            "playcounts_timezone_changed": zone_moved,
            "device_time_context": device_clock,
        }
    except Exception as exc:
        logger.error("Failed to parse iTunesDB: %s", exc)
        if raise_on_error:
            raise
        return _nothing_loaded()


# ── saving ──────────────────────────────────────────────────────────


def _device_traits(ipod_path: Path):
    """The selected device's resolved traits, or ``None`` when nothing usable is selected."""
    import podsync.hardware as hardware

    try:
        device = hardware.selected_device_at(str(ipod_path))
        return device.capabilities if device and device.model_family else None
    except Exception as exc:
        logger.debug("Device traits unavailable for %s: %s", ipod_path, exc)
        return None


def _needs_sqlite(ipod_path: Path, traits) -> bool:
    if traits is not None and getattr(traits, "uses_sqlite_db", False):
        return True
    return os.path.isdir(os.path.join(str(ipod_path), "iPod_Control", "iTunes", "iTunes Library.itlp"))


def save_device_library(
    ipod_path: Path,
    tracks: list[TrackRecord],
    pc_file_paths: dict | None = None,
    playlists: list[PlaylistRecord] | None = None,
    podcast_playlists: list[PlaylistRecord] | None = None,
    smart_playlists: list[PlaylistRecord] | None = None,
    master_playlist_name: str = "iPod",
    master_playlist_id: int | None = None,
    podcast_master_playlist_name: str | None = None,
    podcast_master_playlist_id: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
    raise_on_error: bool = False,
    case_sensitive_paths: bool | None = None,
    before_database_replace: Callable[[], None] | None = None,
    before_device_mutation: Callable[[], None] | None = None,
) -> bool:
    """Write the library, then prove it reads back.  ``True`` only when both succeed.

    SQLite-era devices (nano 5G–7G) are refused up front with
    ``NotImplementedError`` regardless of *raise_on_error*: nothing is written.
    """
    import podsync.itdb.writer as writer

    traits = _device_traits(ipod_path)
    if _needs_sqlite(ipod_path, traits):
        raise NotImplementedError(
            "SQLite databases (iPod nano 5G-7G) are not supported: "
            "save_device_library only writes the classic iTunesDB family"
        )

    logger.info("Writing %d tracks to %s", len(tracks), ipod_path)
    try:
        written = writer.write_itdb(
            str(ipod_path), tracks,
            pc_file_paths=pc_file_paths, playlists=playlists,
            podcast_playlists=podcast_playlists, smart_playlists=smart_playlists,
            capabilities=traits,
            master_playlist_name=master_playlist_name, master_playlist_id=master_playlist_id,
            podcast_master_playlist_name=podcast_master_playlist_name,
            podcast_master_playlist_id=podcast_master_playlist_id,
            progress_callback=progress_callback,
            before_database_replace=before_database_replace,
            before_device_mutation=before_device_mutation,
        )
    except Exception as exc:
        logger.exception(
            "Database write failed during iTunesDB serialization; output was not committed. %s", exc,
        )
        if raise_on_error:
            raise
        return False
    if not written:
        logger.warning("The iTunesDB writer reported failure; nothing to verify")
        return False

    try:
        verify_saved_library(
            ipod_path, expected_track_count=len(tracks), case_sensitive_paths=case_sensitive_paths,
        )
    except ReadbackError as exc:
        logger.error("%s", exc)
        if raise_on_error:
            raise
        return False
    return True


# ── verification ────────────────────────────────────────────────────


def _media_identity_key(path, *, case_sensitive: bool) -> str:
    """Comparable form of a media path: forward slashes, case-folded unless the volume is HFSX."""
    key = str(path).replace("\\", "/")
    return key if case_sensitive else key.casefold()


def _media_problems(ipod_path: Path, tracks: list[dict], case_sensitive: bool) -> list[str]:
    from podsync.library.media_paths import expected_media_path

    root = ipod_path.resolve()
    first_seen: dict[str, str] = {}
    problems: list[str] = []
    for row in tracks:
        title = str(row.get("title") or "?")
        location = str(row.get("location") or "").strip()
        if not location:
            problems.append(f"track '{title}' has no Location")
            continue
        media = expected_media_path(ipod_path, location)
        if media is None:
            problems.append(f"track '{title}' has an invalid or outside the iPod media path {location}")
            continue
        resolved = media.resolve()
        if not resolved.is_relative_to(root):
            problems.append(f"track '{title}' references media outside the iPod {location}")
            continue
        key = _media_identity_key(resolved, case_sensitive=case_sensitive)
        if key in first_seen:
            problems.append(
                f"duplicate media location {location} (already referenced as {first_seen[key]})"
            )
        else:
            first_seen[key] = location
        if not resolved.is_file():
            problems.append(f"track '{title}' references missing media {location}")
    return problems


def verify_saved_library(
    ipod_path: Path, *, expected_track_count: int, case_sensitive_paths: bool | None = None,
) -> None:
    """Re-read the committed database and check counts and every media reference.

    Raises :class:`ReadbackError` describing up to five problems.
    """
    from podsync.hardware.safety.fstype import detect_volume_format

    ipod_path = Path(ipod_path)
    try:
        loaded = load_device_library(ipod_path, raise_on_error=True)
    except Exception as exc:
        raise ReadbackError(f"Freshly written iTunesDB could not be reparsed: {exc}") from exc

    tracks = loaded.get("tracks", [])
    problems: list[str] = []
    if len(tracks) != expected_track_count:
        problems.append(
            f"track count mismatch (expected {expected_track_count}, read back {len(tracks)})"
        )
    if case_sensitive_paths is None:
        case_sensitive_paths = detect_volume_format(ipod_path) == "hfsx"
    problems += _media_problems(ipod_path, tracks, case_sensitive_paths)

    if problems:
        detail = "; ".join(problems[:_MAX_REPORTED_PROBLEMS])
        if len(problems) > _MAX_REPORTED_PROBLEMS:
            detail += f"; and {len(problems) - _MAX_REPORTED_PROBLEMS} more problem(s)"
        raise ReadbackError(f"Freshly written iTunesDB failed verification: {detail}")
    logger.info("Verified freshly written iTunesDB: %d tracks and all media paths exist", len(tracks))


# ── post-commit cleanup ─────────────────────────────────────────────


def clear_device_play_state(
    ipod_path: Path, *, before_device_mutation: Callable[[], None] | None = None,
) -> None:
    """Delete the firmware's play-state files after a successful commit.

    Only the four exact names are touched (``OTGPlaylistInfo_1`` and friends stay).
    The hook runs before each file.  Filesystem or path-safety failures become
    :class:`UnsafeWriteError`; other hook errors propagate unchanged.
    """
    for name in _PLAY_STATE_FILES:
        try:
            if before_device_mutation is not None:
                before_device_mutation()
            target = safe_device_path(ipod_path, f"{_ITUNES_DIR}/{name}", allowed_subtree=_ITUNES_DIR)
            safe_unlink(target, missing_ok=True)
        except (OSError, PathEscapeError) as exc:
            raise UnsafeWriteError(
                "The iPod database was committed, but its device-generated sync state "
                f"could not be cleared ({name}): {exc}"
            ) from exc
        logger.info("Cleared device-generated sync state %s", target)
