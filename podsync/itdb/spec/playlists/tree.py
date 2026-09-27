"""Playlist folders: repair the parent links and derive each folder's contents.

A folder's membership is the union of its children's items (keeping any
order the folder already had for items that are still present), and it is
expressed as an "any of these playlists" smart rule so folders flow through
the same code paths as smart playlists.

Both public functions are pure: they return copied rows in folder pre-order.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from podsync.itdb.spec.playlists.kinds import is_playlist_folder, is_podcast_playlist, playlist_kind_flags

__all__ = ["normalize_playlist_tree", "refresh_playlist_hierarchy_ancestors"]

_PLAYLIST_FIELD = 0x28  # smart-rule field "Playlist"
_RULE_IS = 0x00000001
_SLST_DEFAULT_UNK004 = 0x00010001
_FOLDER_PREFS = {
    "live_update": True,
    "check_rules": True,
    "check_limits": False,
    "limit_type": 3,  # songs
    "limit_sort": 2,  # random
    "limit_value": 25,
    "match_checked_only": False,
}
_ITEM_ID_FIELDS = ("db_track_id", "db_id", "track_persistent_id", "track_id")


def _int_field(row: Mapping, *keys: str) -> int:
    raw: Any = 0
    for key in keys:
        if key in row:
            raw = row[key]
            break
    try:
        return int(raw or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _row_id(row: Mapping) -> int:
    return _int_field(row, "playlist_id")


def _item_key(item: object) -> tuple[str, str]:
    """A stable identity for a playlist entry, whatever ids it happens to carry."""
    if not isinstance(item, Mapping):
        return ("value", repr(item))
    for name in _ITEM_ID_FIELDS:
        value = item.get(name)
        if value not in (None, "", 0, "0"):
            return (name, str(value))
    host_path = item.get("source_path") or item.get("_source_path")
    if host_path:
        return ("source_path", str(host_path).casefold())
    return ("mapping", repr(sorted(item.items(), key=lambda pair: str(pair[0]))))


def _copy_item(item: object) -> object:
    return dict(item) if isinstance(item, Mapping) else item


class _Forest:
    """Copied rows plus the folder structure derived from them."""

    def __init__(self, rows: Iterable[Mapping]) -> None:
        originals = list(rows)
        self.rows = [dict(row) for row in originals]
        self.folder_ids = {_row_id(r) for r in originals if _row_id(r) and is_playlist_folder(r)}
        for row in self.rows:
            self._normalize(row)
        self.cyclic = self._detach_cycles()
        self._parents = {_row_id(r): r["parent_folder_playlist_id"] for r in self.rows}
        self.children: dict[int, list[dict]] = {fid: [] for fid in self.folder_ids}
        for row in self.rows:
            parent = row["parent_folder_playlist_id"]
            if parent in self.folder_ids and parent != _row_id(row):
                self.children[parent].append(row)

    def _normalize(self, row: dict) -> None:
        flags = playlist_kind_flags(row)
        parent = _int_field(row, "parent_folder_playlist_id", "unk0x30_playlist_ref")
        if parent not in self.folder_ids or parent == _row_id(row):
            parent = 0  # dangling or self-referencing parent: move to the top level
        row.update(
            playlist_kind_flags=flags,
            podcast_flag=flags,
            is_folder=is_playlist_folder(flags),
            is_podcast=is_podcast_playlist(flags),
            parent_folder_playlist_id=parent,
            unk0x30_playlist_ref=parent,
        )

    def _detach_cycles(self) -> set[int]:
        """Folders that are their own ancestor are moved to the top level."""
        parent_of = {
            _row_id(r): r["parent_folder_playlist_id"] for r in self.rows if _row_id(r) in self.folder_ids
        }
        cyclic: set[int] = set()
        for start in parent_of:
            trail: list[int] = []
            node = start
            while node in parent_of and node not in trail:
                trail.append(node)
                node = parent_of[node]
            if node in trail:
                cyclic.update(trail[trail.index(node):])
        for row in self.rows:
            if _row_id(row) in cyclic:
                row["parent_folder_playlist_id"] = row["unk0x30_playlist_ref"] = 0
        return cyclic

    def parent_of(self, playlist_id: int) -> int:
        return self._parents.get(playlist_id, 0)

    def ancestors(self, folder_id: int) -> list[int]:
        chain: list[int] = []
        node = self.parent_of(folder_id)
        while node in self.folder_ids and node not in chain:
            chain.append(node)
            node = self.parent_of(node)
        return chain

    def rebuild(self, folder: dict) -> None:
        """Recompute a folder's items and synthesized rules from its direct children."""
        kids = self.children.get(_row_id(folder), [])
        from_children: dict[tuple[str, str], object] = {}
        for child in kids:
            for item in child.get("items") or []:
                from_children.setdefault(_item_key(item), item)

        items: list[object] = []
        placed: set[tuple[str, str]] = set()
        # Keep the folder's own order for entries that are still contributed by a child…
        for item in folder.get("items") or []:
            key = _item_key(item)
            if key in from_children and key not in placed:
                placed.add(key)
                items.append(_copy_item(item))
        # …then append whatever the children add, in child order.
        for key, item in from_children.items():
            if key not in placed:
                placed.add(key)
                items.append(_copy_item(item))
        folder["items"] = items
        folder["mhip_child_count"] = len(items)

        stored = folder.get("smart_playlist_data")
        folder["smart_playlist_data"] = {**_FOLDER_PREFS, **(stored if isinstance(stored, Mapping) else {})}
        folder["smart_playlist_rules"] = {
            "conjunction": "OR",
            "unk004": _SLST_DEFAULT_UNK004,
            "rules": [
                {"field_id": _PLAYLIST_FIELD, "action_id": _RULE_IS,
                 "from_value": cid, "from_units": 1, "to_value": cid, "to_units": 1}
                for cid in map(_row_id, kids) if cid
            ],
        }

    def preorder(self) -> list[dict[str, Any]]:
        """Top-level rows in input order, each followed by its subtree; orphans last."""
        ordered: list[dict[str, Any]] = []
        emitted: set[int] = set()

        def visit(row: dict) -> None:
            if id(row) in emitted:
                return
            emitted.add(id(row))
            ordered.append(row)
            for child in self.children.get(_row_id(row), ()):
                visit(child)

        for row in self.rows:
            if row["parent_folder_playlist_id"] not in self.folder_ids:
                visit(row)
        for row in self.rows:
            visit(row)
        return ordered


def normalize_playlist_tree(rows: Iterable[Mapping]) -> list[dict[str, Any]]:
    """Repair parents, rebuild every folder bottom-up, and return rows in pre-order."""
    forest = _Forest(rows)
    done: set[int] = set()

    def rebuild_subtree(folder: dict, active: frozenset[int]) -> None:
        if id(folder) in done or id(folder) in active:
            return
        for child in forest.children.get(_row_id(folder), ()):
            if _row_id(child) in forest.folder_ids:
                rebuild_subtree(child, active | {id(folder)})
        forest.rebuild(folder)
        done.add(id(folder))

    for row in forest.rows:
        if _row_id(row) in forest.folder_ids:
            rebuild_subtree(row, frozenset())
    return forest.preorder()


def refresh_playlist_hierarchy_ancestors(
    rows: Iterable[Mapping], affected_folder_ids: Iterable[int],
) -> list[dict[str, Any]]:
    """Rebuild only the given folders and their ancestors (deepest first)."""
    forest = _Forest(rows)
    targets = {int(fid) for fid in affected_folder_ids if int(fid) in forest.folder_ids} | forest.cyclic
    for folder_id in list(targets):
        targets.update(forest.ancestors(folder_id))
    by_id = {_row_id(r): r for r in forest.rows if _row_id(r) in forest.folder_ids}
    for folder_id in sorted(targets, key=lambda fid: len(forest.ancestors(fid)), reverse=True):
        if folder_id in by_id:
            forest.rebuild(by_id[folder_id])
    return forest.preorder()
