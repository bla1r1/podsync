"""Turn parsed playlist rows into :class:`PlaylistRecord` objects ready for the writer.

Three row lists come from the device: the visible playlists (dataset 2), their
podcast-aware mirror (dataset 3) and the category/smart list (dataset 5).
:func:`assemble_playlists` resolves each row's items to database track ids,
re-evaluates live smart playlists, keeps the Podcasts playlist in step with the
podcast tracks, and applies each playlist's sort order (the firmware does not).
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from podsync.itdb.spec.clock import DeviceClock
from podsync.itdb.spec.codes import MEDIA_TYPE_PODCAST
from podsync.itdb.spec.playlists.properties import (
    playlist_description_from_row,
    playlist_property_raw_body_for_write,
)
from podsync.itdb.spec.playlists.tree import normalize_playlist_tree
from podsync.itdb.writer.playlist import EntryMeta, PlaylistRecord
from podsync.itdb.writer.smart_rules import prefs_from_row, rules_from_row
from podsync.itdb.writer.track import TrackRecord
from podsync.library.paths import as_int, path_key

__all__ = ["assemble_playlists", "blob_from_cache", "order_rows", "order_track_ids"]

logger = logging.getLogger(__name__)

# ── sort orders ─────────────────────────────────────────────────────


class _SortKey(NamedTuple):
    field: str  # row key
    text: bool  # compare case-folded text instead of numbers
    override: str | None = None  # row key that replaces `field` when non-empty


def _t(name: str, override: str | None = None) -> _SortKey:
    return _SortKey(name, True, override)


def _n(name: str) -> _SortKey:
    return _SortKey(name, False)


_ARTIST, _ALBUM = _t("artist", "sort_artist"), _t("album", "sort_album")
_DISC_TRACK = (_n("disc_number"), _n("track_number"))

# MHYP sort-order code -> key sequence.  0 (unset) and 1 (manual) keep the stored order.
_SORT_ORDERS: dict[int, tuple[_SortKey, ...]] = {
    3: (_t("title", "sort_title"),),
    4: (_ALBUM, *_DISC_TRACK),
    5: (_ARTIST, _ALBUM, *_DISC_TRACK),
    6: (_n("bitrate"),),
    7: (_t("genre"), _ARTIST, _ALBUM, _n("track_number")),
    8: (_t("filetype"),),
    9: (_n("last_modified"),),
    10: _DISC_TRACK,
    11: (_n("size"),),
    12: (_n("length"),),
    13: (_n("year"), _ARTIST, _ALBUM),
    14: (_n("sample_rate"),),
    15: (_t("comment"),),
    16: (_n("date_added"),),
    17: (_t("eq_setting"),),
    18: (_t("composer"),),
    20: (_n("play_count"),),
    21: (_n("last_played"),),
    22: _DISC_TRACK,
    23: (_n("rating"),),
    24: (_n("date_released"),),
    25: (_n("bpm"),),
    26: (_t("grouping"),),
}

# Row key -> TrackRecord attribute, for sorting records instead of rows.
_RECORD_ATTRIBUTE = {"sample_rate": "sample_rate", "play_count": "play_count"}


def _normalize(value: Any, text: bool) -> Any:
    if text:
        return "" if value is None else str(value).casefold()
    return value if isinstance(value, (int, float)) else 0


def _row_sort_key(keys: tuple[_SortKey, ...]) -> Callable[[Mapping[str, Any]], tuple]:
    def key(row: Mapping[str, Any]) -> tuple:
        return tuple(
            _normalize((row.get(k.override) if k.override else None) or row.get(k.field), k.text)
            for k in keys
        )
    return key


def _record_sort_key(keys: tuple[_SortKey, ...]) -> Callable[[TrackRecord], tuple]:
    # Records carry no separate sort-override attributes in this lookup; use the base field.
    def key(record: TrackRecord) -> tuple:
        return tuple(
            _normalize(getattr(record, _RECORD_ATTRIBUTE.get(k.field, k.field), None), k.text)
            for k in keys
        )
    return key


def order_rows(tracks: list[dict], sort_order: int) -> list[dict]:
    """Sort parsed rows by an MHYP sort order; manual/unknown orders return the same list."""
    keys = _SORT_ORDERS.get(sort_order)
    return sorted(tracks, key=_row_sort_key(keys)) if keys else tracks


def order_track_ids(
    track_ids: list[int], sort_order: int, db_track_id_to_info: dict[int, TrackRecord],
) -> list[int]:
    """Sort database ids by their records; ids without a record keep their order at the end."""
    keys = _SORT_ORDERS.get(sort_order)
    if not keys:
        return track_ids
    record_key = _record_sort_key(keys)
    known = sorted(
        (tid for tid in track_ids if tid in db_track_id_to_info),
        key=lambda tid: record_key(db_track_id_to_info[tid]),
    )
    return known + [tid for tid in track_ids if tid not in db_track_id_to_info]


def blob_from_cache(value) -> bytes | None:
    """Accept raw MHOD bytes as-is or base64 text (JSON caches); anything else is ``None``."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        try:
            return base64.b64decode(value)
        except Exception:
            return None
    return None


# ── row helpers ─────────────────────────────────────────────────────


def _description(row: Mapping[str, Any]) -> str | None:
    """Keep an explicitly empty description as ``""``; absent means ``None``."""
    text = playlist_description_from_row(row)
    return text if text or "playlist_description" in row else None


def _shared_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """Constructor arguments common to every dataset."""
    return {
        "name": row.get("title", "Untitled"),
        "playlist_id": row.get("playlist_id"),
        "sortorder": row.get("sort_order", 0),
        "mhsd5_type": as_int(row.get("mhsd5_type", 0)),
        "phase_game_flag": as_int(row.get("phase_game_flag", 0)),
        "raw_mhod100": blob_from_cache(row.get("playlist_prefs")),
        "raw_mhod102": blob_from_cache(row.get("playlist_settings")),
        "raw_mhod55": playlist_property_raw_body_for_write(row),
        "playlist_description": _description(row),
    }


def _attach_rules(record: PlaylistRecord, row: Mapping[str, Any]) -> None:
    prefs, rules = row.get("smart_playlist_data"), row.get("smart_playlist_rules")
    if prefs and rules:
        record.smart_prefs = prefs_from_row(prefs)
        record.smart_rules = rules_from_row(rules)


def _is_live(record: PlaylistRecord) -> bool:
    return bool(
        record.smart_prefs and record.smart_rules
        and record.smart_prefs.live_update and not record.is_folder
    )


# ── the assembler ───────────────────────────────────────────────────


@dataclass
class _Assembler:
    """Shared state for resolving and evaluating one library's playlists."""

    id_by_parser_track: dict[int, int]
    valid_ids: set[int]
    rule_views: list[dict]
    ids_by_source_path: dict[str, int]
    evaluate: Callable[..., list[int]]
    clock: DeviceClock | None
    seed_lookup: dict[int, set[int]] = field(default_factory=dict)

    def resolve_item(self, item: Mapping[str, Any]) -> int:
        """Database id for one MHIP row: parser id → cached db id → host path."""
        db_id = self.id_by_parser_track.get(as_int(item.get("track_id", 0)), 0)
        if not db_id:
            db_id = item.get("db_track_id", item.get("db_id", 0))
        if not as_int(db_id):
            host_path = item.get("source_path") or item.get("_source_path")
            db_id = self.ids_by_source_path.get(path_key(str(host_path)), 0) if host_path else 0
        return as_int(db_id)

    def members(self, items: Iterable[Any] | None) -> tuple[list[int], list[EntryMeta] | None]:
        """Resolved ids of a row's items plus their MHIP details; dangling refs are dropped."""
        ids: list[int] = []
        details: list[EntryMeta] = []
        for item in items or ():
            if not isinstance(item, dict):
                continue
            db_id = self.resolve_item(item)
            if db_id not in self.valid_ids:
                continue
            ids.append(db_id)
            details.append(EntryMeta(
                podcast_group_flag=item.get("podcast_group_flag", 0),
                group_id=item.get("group_id", 0),
                podcast_group_ref=item.get("group_link", 0),
                track_persistent_id=item.get("track_persistent_id", 0),
                mhip_persistent_id=item.get("mhip_persistent_id", 0),
            ))
        return ids, details or None

    def lookup_from_rows(self, *row_lists: list[dict]) -> dict[int, set[int]]:
        """Playlist id → member ids, for "is in playlist" rules.  Masters are skipped."""
        lookup: dict[int, set[int]] = {}
        for rows in row_lists:
            for row in rows:
                raw_id = row.get("playlist_id")
                if row.get("master_flag") or not raw_id:
                    continue
                try:
                    key = int(raw_id)
                except (TypeError, ValueError):
                    continue
                ids, _details = self.members(row.get("items"))
                lookup.setdefault(key, set()).update(ids)
        return lookup

    def rescore(self, record: PlaylistRecord, lookup: Mapping[int, set[int]]) -> list[int]:
        matched = self.evaluate(
            record.smart_prefs, record.smart_rules, self.rule_views, lookup, self.clock,
        )
        return [db_id for db_id in matched if db_id in self.valid_ids]

    def visible_dataset(
        self, rows: list[dict], dataset: str,
    ) -> tuple[str, int | None, list[PlaylistRecord]]:
        """Dataset 2 or 3: split off the master row, build the rest."""
        masters = [row for row in rows if row.get("master_flag")]
        if len(masters) > 1:
            raise ValueError(f"{dataset} contains {len(masters)} master_flag playlist rows")
        master_name = masters[0].get("title", "iPod") if masters else "iPod"
        master_id = masters[0].get("playlist_id") if masters else None

        built: list[PlaylistRecord] = []
        for row in rows:
            if row.get("master_flag"):
                continue
            ids, details = self.members(row.get("items"))
            record = PlaylistRecord(
                **_shared_fields(row),
                track_ids=ids,
                item_metadata=details,
                master=False,
                podcast_flag=row.get("podcast_flag", 0),
                playlist_kind_flags=row.get("playlist_kind_flags"),
                parent_folder_playlist_id=as_int(
                    row.get("parent_folder_playlist_id", row.get("unk0x30_playlist_ref", 0))
                ),
            )
            _attach_rules(record, row)
            # Folders keep their aggregated membership; static smart lists keep their snapshot.
            if _is_live(record):
                record.track_ids = self.rescore(record, self.seed_lookup)
                record.item_metadata = None
            built.append(record)
        return master_name, master_id, built

    def category_dataset(self, rows: list[dict]) -> list[PlaylistRecord]:
        """Dataset 5: categories (non-zero ``mhsd5_type``) keep what the device stored."""
        built: list[PlaylistRecord] = []
        for row in rows:
            ids, details = self.members(row.get("items"))
            record = PlaylistRecord(
                **_shared_fields(row),
                track_ids=ids,
                item_metadata=details,
                master=bool(row.get("master_flag", 0)),
            )
            _attach_rules(record, row)
            if not record.mhsd5_type and record.smart_prefs is not None and record.smart_prefs.live_update:
                record.track_ids = self.rescore(record, self.seed_lookup)
                record.item_metadata = None
            built.append(record)
        return built

    def refresh_live(self, visible: list[PlaylistRecord], podcast: list[PlaylistRecord],
                     categories: list[PlaylistRecord]) -> None:
        """Second pass so "is in playlist" rules see other playlists' final membership."""
        for record in [*visible, *podcast, *(c for c in categories if not c.mhsd5_type)]:
            if not _is_live(record):
                continue
            lookup = _lookup_from_records(visible, categories)
            fresh = self.rescore(record, lookup)
            if fresh != record.track_ids:
                record.track_ids = fresh
                record.item_metadata = None


def _lookup_from_records(*groups: Iterable[PlaylistRecord]) -> dict[int, set[int]]:
    lookup: dict[int, set[int]] = {}
    for group in groups:
        for record in group:
            if record.playlist_id is None or record.master:
                continue
            lookup.setdefault(int(record.playlist_id), set()).update(record.track_ids)
    return lookup


def _mirror_podcasts(
    visible: list[PlaylistRecord], podcast: list[PlaylistRecord], records: list[TrackRecord],
) -> None:
    """Every podcast-kind playlist lists exactly the podcast tracks, in library order."""
    episodes = [
        r.db_track_id for r in records
        if r.db_track_id and (r.media_type & MEDIA_TYPE_PODCAST or r.podcast_flag)
    ]
    targets = [p for p in (*visible, *podcast) if p.is_podcast]
    if not targets and episodes:
        created = PlaylistRecord(name="Podcasts", track_ids=[], podcast_flag=1)
        podcast.append(created)
        targets.append(created)
        logger.info("Created a Podcasts playlist for %d episode(s)", len(episodes))
    for target in targets:
        if target.track_ids != episodes:
            target.track_ids = list(episodes)
            target.item_metadata = None


def assemble_playlists(
    existing_tracks_data: list[dict],
    dataset2_standard_playlists_raw: list[dict],
    dataset3_podcast_playlists_raw: list[dict],
    dataset5_smart_playlists_raw: list[dict],
    all_track_infos: list[TrackRecord],
    source_path_to_db_track_id: dict[str, int] | None = None,
    time_context: DeviceClock | None = None,
) -> tuple[str, int | None, list[PlaylistRecord], str, int | None, list[PlaylistRecord], list[PlaylistRecord]]:
    """Build the writer's playlist inputs from parsed rows and the final track records.

    Returns ``(ds2 master name, ds2 master id, ds2 playlists, ds3 master name,
    ds3 master id, ds3 playlists, ds5 playlists)``.  Input rows are not mutated.
    """
    # Looked up at call time so tests can substitute them.
    from podsync.library.smart import evaluate_smart_playlist
    from podsync.library.tracks import rule_view_of

    visible_rows = normalize_playlist_tree(dataset2_standard_playlists_raw or [])
    podcast_rows = normalize_playlist_tree(dataset3_podcast_playlists_raw or [])
    category_rows = list(dataset5_smart_playlists_raw or [])

    id_by_parser_track = {}
    for row in existing_tracks_data or []:
        parser_id = as_int(row.get("track_id"))
        db_id = as_int(row.get("db_track_id") or row.get("db_id"))
        if parser_id and db_id:
            id_by_parser_track[parser_id] = db_id

    ids_by_source_path = {
        path_key(str(r.source_path)): as_int(r.db_track_id)
        for r in all_track_infos if r.source_path and as_int(r.db_track_id)
    }
    # Caller-supplied paths win over what the records carry.
    ids_by_source_path.update(
        {path_key(str(raw)): as_int(db_id) for raw, db_id in (source_path_to_db_track_id or {}).items()}
    )

    assembler = _Assembler(
        id_by_parser_track=id_by_parser_track,
        valid_ids={as_int(r.db_track_id) for r in all_track_infos} - {0},
        rule_views=[rule_view_of(r) for r in all_track_infos],
        ids_by_source_path=ids_by_source_path,
        evaluate=evaluate_smart_playlist,
        clock=time_context,
    )
    assembler.seed_lookup = assembler.lookup_from_rows(visible_rows, podcast_rows, category_rows)

    ds2_name, ds2_id, visible = assembler.visible_dataset(visible_rows, "dataset2")
    ds3_name, ds3_id, podcast = assembler.visible_dataset(podcast_rows, "dataset3")
    categories = assembler.category_dataset(category_rows)

    assembler.refresh_live(visible, podcast, categories)
    _mirror_podcasts(visible, podcast, all_track_infos)

    by_db_id = {r.db_track_id: r for r in all_track_infos if r.db_track_id}
    for record in (*visible, *podcast, *categories):
        if record.sortorder not in (0, 1) and record.track_ids:
            record.track_ids = order_track_ids(record.track_ids, record.sortorder, by_db_id)
            record.item_metadata = None  # positional MHIP details no longer line up

    logger.debug(
        "Assembled playlists: %d visible, %d podcast, %d category",
        len(visible), len(podcast), len(categories),
    )
    return ds2_name, ds2_id, visible, ds3_name, ds3_id, podcast, categories
