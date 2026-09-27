"""Write cover art for a whole library: decide per track, encode, pack ``.ithmb`` files, index.

For every track the writer decides to *preserve* the art already on the
device, *replace* it with art found next to the source file, or *clear* it.
Preserved renditions are copied (or, when their file is not being rewritten,
referenced in place); broken ones are salvaged by decoding another rendition
and re-encoding.  New pixels go into ``F<format>_<n>.ithmb`` shards capped at
:data:`ITHMB_MAX_SIZE_BYTES`, all staged as temp files and swapped in only on
commit — optionally deferred until the iTunesDB is committed too.
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from podsync.artwork.spec.files import ithmb_filename, ithmb_filename_from_path
from podsync.artwork.writer.chunks import build_artworkdb, read_existing_artwork
from podsync.artwork.writer.codecs import (
    decode_pixels_for_format,
    default_stride_pixels,
    encode_image_for_format,
    expected_size_bytes,
    format_dimensions,
)
from podsync.artwork.writer.covers import art_hash, extract_art_with_source
from podsync.artwork.writer.covers import extract_art_with_folder as _default_extract_art_with_folder
from podsync.artwork.writer.pixels import get_artwork_format_definitions, get_artwork_formats, image_from_bytes
from podsync.artwork.writer.records import (
    ArtworkEntry,
    ArtworkPayload,
    EncodedFormatPayload,
    ExistingFormatRef,
    IthmbLocation,
    PassthroughFormatRef,
)
from podsync.hardware import ITHMB_FORMAT_MAP
from podsync.hardware.catalog.artwork_presets import ArtworkFormat
from podsync.hardware.safety.durable import flush_written_file, open_unique_sibling_temp, safe_replace, safe_unlink
from podsync.hardware.safety.paths import safe_device_path

__all__ = [
    "ITHMB_MAX_SIZE_BYTES", "ArtworkAssetRef", "ArtworkDecisionKind", "ArtworkDecisionSummary", "ArtworkEntry",
    "ArtworkFormat", "ArtworkPayload", "EncodedFormatPayload", "ExistingArtworkFormats", "ExistingFormatRef",
    "IthmbLocation", "PassthroughFormatRef", "PendingArtworkWrite", "TrackArtworkDecision", "write_artworkdb",
]

logger = logging.getLogger(__name__)

ITHMB_MAX_SIZE_BYTES = 32 * 1000 * 1000

# Substitutable: when replaced, it is used instead of the (art, source) variant.
extract_art_with_folder = _default_extract_art_with_folder
_TRACK_ARTWORK_KEYS = ("mhii_link", "mhiiLink", "artwork_link")

# ── models ──────────────────────────────────────────────────────────


class ArtworkDecisionKind(StrEnum):
    NEW_FROM_PC = "new_from_pc"
    PRESERVE_EXISTING = "preserve_existing"
    CLEAR_ART = "clear_art"
    PRESERVE_FALLBACK = "preserve_fallback"


_PRESERVE_KINDS = frozenset({ArtworkDecisionKind.PRESERVE_EXISTING, ArtworkDecisionKind.PRESERVE_FALLBACK})


@dataclass(frozen=True)
class ArtworkAssetRef:
    """Identity of one picture: ``("pc", content hash)`` or ``("preserve", stored layout)``."""

    source: str
    value: str | int


@dataclass
class TrackArtworkDecision:
    db_track_id: int
    kind: ArtworkDecisionKind
    asset_ref: ArtworkAssetRef | None = None
    art_bytes: bytes | None = None
    src_img_size: int = 0
    source_path: str = ""
    existing_entry: dict | None = None


@dataclass
class ArtworkDecisionSummary:
    preserved_unchanged: int = 0
    preserved_fallback: int = 0
    reencoded: int = 0
    cleared: int = 0
    shared_from_album: int = 0
    salvaged: int = 0
    dropped_invalid: int = 0


@dataclass
class ExistingArtworkFormats:
    """An existing entry's renditions, grouped by how the writer must treat them."""

    required_known: dict[int, ExistingFormatRef] = field(default_factory=dict)
    extra_known: dict[int, ExistingFormatRef] = field(default_factory=dict)
    unknown_passthrough: dict[int, PassthroughFormatRef] = field(default_factory=dict)
    known_present: set[int] = field(default_factory=set)


@dataclass
class PendingArtworkWrite:
    """Staged artwork files waiting to be swapped in; behaves like the ``db_track_id → (img_id, size)`` map."""

    db_track_id_to_art_info: dict
    _pending_renames: list = field(default_factory=list)
    _post_commit_cleanup: Callable[[], None] | None = None
    _committed: bool = False

    @property
    def db_id_to_art_info(self) -> dict:
        return self.db_track_id_to_art_info

    def __getitem__(self, key):
        return self.db_track_id_to_art_info[key]

    def __setitem__(self, key, value) -> None:
        self.db_track_id_to_art_info[key] = value

    def __contains__(self, key) -> bool:
        return key in self.db_track_id_to_art_info

    def __iter__(self):
        return iter(self.db_track_id_to_art_info)

    def __len__(self) -> int:
        return len(self.db_track_id_to_art_info)

    def get(self, key, default=None):
        return self.db_track_id_to_art_info.get(key, default)

    def keys(self):
        return self.db_track_id_to_art_info.keys()

    def values(self):
        return self.db_track_id_to_art_info.values()

    def items(self):
        return self.db_track_id_to_art_info.items()

    def commit(self, before_replace: Callable[[], None] | None = None) -> None:
        """Swap every staged file into place (hook before each swap); idempotent."""
        if self._committed:
            return
        for temp, final in self._pending_renames:
            if before_replace is not None:
                before_replace()
            safe_replace(temp, final)
        if self._post_commit_cleanup is not None:
            self._post_commit_cleanup()
        self._committed = True

    def abort(self, before_remove: Callable[[], None] | None = None) -> None:
        """Delete every staged file; a no-op after commit."""
        if self._committed:
            return
        for temp, _final in self._pending_renames:
            try:
                if before_remove is not None:
                    before_remove()
                safe_unlink(temp, missing_ok=True)
            except OSError:
                pass


# ── small helpers ───────────────────────────────────────────────────


def _field(track: Any, name: str) -> Any:
    if name == "db_track_id":
        if isinstance(track, Mapping):
            return track.get("db_track_id", track.get("db_id"))
        return getattr(track, "db_track_id", getattr(track, "db_id", None))
    return track.get(name) if isinstance(track, Mapping) else getattr(track, name, None)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _ref_filename(ref: Any, fmt_id: int) -> str:
    return getattr(ref, "ithmb_filename", "") or ithmb_filename_from_path(ref.path, fmt_id)


def _known_format(fmt_id: int, device_format_defs: Mapping) -> Any:
    return device_format_defs.get(fmt_id) or ITHMB_FORMAT_MAP.get(fmt_id)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'s' if count != 1 else ''}"


def _usable_file(ref: Any) -> str | None:
    path = str(getattr(ref, "path", "") or "")
    return path if path and _int(getattr(ref, "size", 0)) > 0 and os.path.exists(path) else None


# ── decisions ───────────────────────────────────────────────────────


def _preserved_asset_ref(existing_img_id: int, existing_entry: dict) -> ArtworkAssetRef:
    """Identity of stored art by its exact layout, so shared renditions dedupe."""
    formats = existing_entry.get("formats") or {}
    layout = [
        f"{fmt_id}:{_ref_filename(formats[fmt_id], int(fmt_id))}:{formats[fmt_id].ithmb_offset}:{formats[fmt_id].size}"
        for fmt_id in sorted(formats, key=_int)
    ]
    return ArtworkAssetRef("preserve", "|".join(layout) if layout else existing_img_id)


class _Decider:
    """Chooses preserve / replace / clear for each track."""

    def __init__(self, pc_file_paths: dict[int, str], existing_art: dict[int, dict]) -> None:
        self.pc_file_paths = pc_file_paths
        self.existing_art = existing_art
        self.summary = ArtworkDecisionSummary()
        self.first_image_of_song: dict[int, int] = {}
        for img_id, entry in existing_art.items():
            song = _int(entry.get("song_id"))
            if song:
                self.first_image_of_song.setdefault(song, img_id)
        self._extracted: dict[str, tuple[bytes | None, str | None]] = {}

    def _existing_image_id(self, track: Any, db_track_id: int) -> int | None:
        """The stored image of this track: its own link if it points back at it, else by song id."""
        link = next((_int(_field(track, key)) for key in _TRACK_ARTWORK_KEYS if _int(_field(track, key))), 0)
        if link and link in self.existing_art and _int(self.existing_art[link].get("song_id")) == db_track_id:
            return link
        if db_track_id in self.first_image_of_song:
            return self.first_image_of_song[db_track_id]
        return link if link and link in self.existing_art else None

    def _extract(self, pc_path: str) -> tuple[bytes | None, str | None]:
        key = os.path.normcase(os.path.abspath(pc_path))
        if key not in self._extracted:
            if extract_art_with_folder is not _default_extract_art_with_folder:
                art = extract_art_with_folder(pc_path)
                self._extracted[key] = (art, pc_path if art else None)
            else:
                self._extracted[key] = extract_art_with_source(pc_path)
        return self._extracted[key]

    def decide(self, track: Any) -> TrackArtworkDecision | None:
        db_track_id = _int(_field(track, "db_track_id"))
        title = _field(track, "title") or "?"
        if not db_track_id:
            logger.warning("ART: track '%s' has no db_track_id, skipping", title)
            return None
        image_id = self._existing_image_id(track, db_track_id)
        existing = self.existing_art.get(image_id) if image_id is not None else None

        def preserve(kind: ArtworkDecisionKind, counter: str) -> TrackArtworkDecision:
            setattr(self.summary, counter, getattr(self.summary, counter) + 1)
            return TrackArtworkDecision(
                db_track_id=db_track_id, kind=kind, asset_ref=_preserved_asset_ref(image_id, existing),
                src_img_size=_int(existing.get("src_img_size")), existing_entry=existing,
            )

        def clear() -> TrackArtworkDecision:
            self.summary.cleared += 1
            return TrackArtworkDecision(db_track_id=db_track_id, kind=ArtworkDecisionKind.CLEAR_ART, existing_entry=existing)

        def keep_or_clear() -> TrackArtworkDecision:
            if existing is not None:
                return preserve(ArtworkDecisionKind.PRESERVE_FALLBACK, "preserved_fallback")
            return clear()

        hint = str(_field(track, "_iop_artwork_sync_hint") or "").strip().lower()
        pc_path = self.pc_file_paths.get(db_track_id)
        if hint == "clear_art":
            return clear()
        if hint == "preserve_existing" and existing is not None:
            if pc_path and os.path.exists(pc_path):
                return preserve(ArtworkDecisionKind.PRESERVE_EXISTING, "preserved_unchanged")
            return preserve(ArtworkDecisionKind.PRESERVE_FALLBACK, "preserved_fallback")
        if not pc_path:
            return keep_or_clear()
        if not os.path.exists(pc_path):
            logger.warning("ART: PC file not found for '%s': %s", title, pc_path)
            return keep_or_clear()
        art, source = self._extract(pc_path)
        if art is None:
            return clear()
        self.summary.reencoded += 1
        return TrackArtworkDecision(
            db_track_id=db_track_id, kind=ArtworkDecisionKind.NEW_FROM_PC,
            asset_ref=ArtworkAssetRef("pc", art_hash(art)), art_bytes=art, src_img_size=len(art),
            source_path=source or pc_path, existing_entry=existing,
        )


def _collect_track_artwork_decisions(
    tracks: list, pc_file_paths: dict[int, str], existing_art: dict[int, dict],
) -> tuple[dict[int, TrackArtworkDecision], ArtworkDecisionSummary]:
    decider = _Decider(pc_file_paths, existing_art)
    decisions: dict[int, TrackArtworkDecision] = {}
    for track in tracks:
        decision = decider.decide(track)
        if decision is not None:
            decisions[decision.db_track_id] = decision
    return decisions, decider.summary


# ── classifying stored renditions ───────────────────────────────────


def _classify_existing_entry_formats(
    existing_entry: dict | None, required_format_ids, device_format_defs: Mapping,
) -> ExistingArtworkFormats:
    """Split an entry's renditions into required, extra-known and unknown (pass-through) ones."""
    grouped = ExistingArtworkFormats()
    if existing_entry is None:
        return grouped
    required = set(required_format_ids)
    for fmt_id, ref in (existing_entry.get("formats") or {}).items():
        if isinstance(fmt_id, bool) or not isinstance(fmt_id, int):
            logger.warning("ART: ignoring non-integer artwork format id %r", fmt_id)
            continue
        if _known_format(fmt_id, device_format_defs) is None:
            if _usable_file(ref) is None:
                logger.warning(
                    "ART: cannot preserve passthrough format %s as-is; missing file or size (%s)",
                    fmt_id, str(getattr(ref, "path", "") or ""),
                )
            else:
                grouped.unknown_passthrough[fmt_id] = (
                    ref if isinstance(ref, PassthroughFormatRef) else PassthroughFormatRef.from_existing_ref(ref)
                )
            continue
        grouped.known_present.add(fmt_id)
        if _usable_file(ref) is None:
            logger.warning(
                "ART: existing known format %s cannot be preserved; missing file or size (%s)",
                fmt_id, str(getattr(ref, "path", "") or ""),
            )
            logger.warning(
                "ART: existing format %s has invalid geometry or payload size; will attempt regeneration if needed",
                fmt_id,
            )
            continue
        _note_unexpected_size(fmt_id, ref, device_format_defs)
        (grouped.required_known if fmt_id in required else grouped.extra_known)[fmt_id] = ref
    return grouped


def _note_unexpected_size(fmt_id: int, ref: Any, device_format_defs: Mapping) -> None:
    """Stored sizes outside the expected ones are carried forward, but noted."""
    override = device_format_defs.get(fmt_id)
    stride, stored_height = max(1, ref.width + ref.hpad), max(1, ref.height + ref.vpad)
    plausible = {
        expected_size_bytes(fmt_id, stride, stored_height, stride_pixels=stride, fmt_override=override),
        expected_size_bytes(
            fmt_id, ref.width, stored_height,
            stride_pixels=default_stride_pixels(fmt_id, ref.width, fmt_override=override), fmt_override=override,
        ),
    } - {0}
    if ref.size not in plausible:
        logger.debug(
            "ART: existing known format %s has size %s outside expected sizes %s; carrying forward on-device bytes",
            fmt_id, ref.size, sorted(plausible),
        )


def _collect_rewrite_targets(
    decisions: dict[int, TrackArtworkDecision], required_format_ids, device_format_defs: Mapping,
) -> tuple[dict[ArtworkAssetRef, list[int]], dict[ArtworkAssetRef, dict[int, PassthroughFormatRef]]]:
    """Per picture: which formats to produce, and which unknown renditions to carry through."""
    targets: dict[ArtworkAssetRef, list[int]] = {}
    passthrough: dict[ArtworkAssetRef, dict[int, PassthroughFormatRef]] = {}
    announced: set = set()
    for decision in decisions.values():
        grouped = _classify_existing_entry_formats(decision.existing_entry, required_format_ids, device_format_defs)
        for kind, bucket, message in (
            ("unknown", grouped.unknown_passthrough,
             "ART: encountered unknown artwork format %s at %s; leaving its ithmb file untouched"),
            ("extra", grouped.extra_known,
             "ART: encountered extra known artwork format %s at %s; preserving/regenerating it because it is present on-device"),
        ):
            for fmt_id, ref in bucket.items():
                if (kind, fmt_id, ref.path) not in announced:
                    announced.add((kind, fmt_id, ref.path))
                    logger.warning(message, fmt_id, ref.path)
        if decision.asset_ref is None:
            continue
        targets[decision.asset_ref] = sorted(set(required_format_ids) | grouped.known_present)
        if decision.kind in _PRESERVE_KINDS:
            passthrough[decision.asset_ref] = dict(grouped.unknown_passthrough)
    return targets, passthrough


# ── producing payloads ──────────────────────────────────────────────


def _target_size(fmt_id: int, device_formats: Mapping, device_format_defs: Mapping) -> tuple[int, int] | None:
    if fmt_id in device_formats:
        width, height = device_formats[fmt_id]
        return int(width), int(height)
    known = _known_format(fmt_id, device_format_defs)
    return (int(known.width), int(known.height)) if known is not None else None


def _load_image(art_bytes: bytes, source_path: str):
    try:
        return image_from_bytes(art_bytes, source_path=source_path)
    except TypeError as exc:  # a substitute without the keyword
        if "source_path" not in str(exc):
            raise
        return image_from_bytes(art_bytes)


def _encode_all(image, format_ids, device_formats, device_format_defs, required, asset, *, salvage: bool) -> dict[int, Any]:
    """Encode *image* into each format; failures are logged (loudly for required formats)."""
    encoded: dict[int, Any] = {}
    for fmt_id in format_ids:
        size = _target_size(fmt_id, device_formats, device_format_defs)
        if size is None:
            if salvage:
                logger.warning("ART: format %s is not encodable for preserved artwork %s; skipping", fmt_id, asset)
            else:
                logger.warning("ART: format %s is not encodable for %s; skipping", fmt_id, asset)
            continue
        try:
            encoded[fmt_id] = encode_image_for_format(image, fmt_id, *size, fmt_override=device_format_defs.get(fmt_id))
        except Exception as exc:
            log = logger.warning if fmt_id in required else logger.debug
            if salvage:
                log("ART: salvage re-encode failed for %s fmt %s: %s", asset, fmt_id, exc)
            else:
                log("ART: format %s conversion failed for %s: %s", fmt_id, asset, exc)
    return encoded


def _convert_new_pc_art(
    decisions: dict[int, TrackArtworkDecision], required_format_ids, asset_target_format_ids: Mapping,
    device_formats: Mapping, device_format_defs: Mapping, progress: Callable[[str], None],
) -> dict[ArtworkAssetRef, ArtworkPayload]:
    """Encode every distinct new picture into all its target formats (in parallel)."""
    pictures: dict[ArtworkAssetRef, TrackArtworkDecision] = {}
    for decision in decisions.values():
        if decision.kind == ArtworkDecisionKind.NEW_FROM_PC and decision.asset_ref is not None:
            pictures.setdefault(decision.asset_ref, decision)
    if not pictures:
        return {}
    progress(f"Artwork — converting {_plural(len(pictures), 'image')}")
    required = set(required_format_ids)

    def convert(item):
        asset, decision = item
        image = _load_image(decision.art_bytes or b"", decision.source_path)
        if image is None:
            return asset, None
        formats = _encode_all(
            image, asset_target_format_ids.get(asset, list(required_format_ids)),
            device_formats, device_format_defs, required, asset, salvage=False,
        )
        missing = sorted(required - set(formats))
        if missing:
            logger.warning(
                "ART: dropping rewritten artwork %s because required formats %s could not be generated", asset, missing,
            )
            return asset, None
        return asset, ArtworkPayload(formats=formats, src_img_size=len(decision.art_bytes or b""))

    with ThreadPoolExecutor(max_workers=max(1, min(len(pictures), os.cpu_count() or 4))) as pool:
        converted = list(pool.map(convert, pictures.items()))
    return {asset: payload for asset, payload in converted if payload is not None}


def _decode_preserved_frame(ref: Any, fmt_id: int, pixel_bytes: bytes, fmt_override: Any = None):
    return decode_pixels_for_format(fmt_id, pixel_bytes, ref.width, ref.height, ref.hpad, ref.vpad, fmt_override=fmt_override)


def _read_ref_bytes(ref: Any) -> bytes | None:
    with open(ref.path, "rb") as handle:
        handle.seek(ref.ithmb_offset)
        data = handle.read(ref.size)
    return data if len(data) == ref.size else None


def _salvage_source(decision: TrackArtworkDecision, device_format_defs: Mapping):
    """Decode the first readable known rendition of a stored picture."""
    for fmt_id, ref in ((decision.existing_entry or {}).get("formats") or {}).items():
        if not isinstance(fmt_id, int) or _known_format(fmt_id, device_format_defs) is None:
            continue
        try:
            data = _read_ref_bytes(ref)
        except OSError:
            continue
        if data is None:
            continue
        image = _decode_preserved_frame(ref, fmt_id, data, fmt_override=device_format_defs.get(fmt_id))
        if image is not None:
            return image
    return None


def _load_preserved_art_payloads(
    decisions: dict[int, TrackArtworkDecision],
    required_format_ids,
    asset_target_format_ids: Mapping,
    asset_passthrough_format_refs: Mapping,
    device_formats: Mapping,
    device_format_defs: Mapping,
    *,
    passthrough_known_formats: bool = False,
    rewrite_known_filenames: Mapping[int, set[str]] | None = None,
) -> tuple[dict[ArtworkAssetRef, ArtworkPayload], int, int]:
    """Payloads for preserved pictures; returns ``(payloads, salvaged count, dropped count)``.

    With *passthrough_known_formats*, renditions whose file is not being rewritten
    are referenced in place instead of copied.
    """
    rewrite = rewrite_known_filenames or {}
    required = set(required_format_ids)
    pictures: dict[ArtworkAssetRef, TrackArtworkDecision] = {}
    for decision in decisions.values():
        if decision.kind in _PRESERVE_KINDS and decision.asset_ref is not None:
            pictures.setdefault(decision.asset_ref, decision)

    def stays_in_place(ref: Any, fmt_id: int) -> bool:
        return passthrough_known_formats and _ref_filename(ref, fmt_id) not in rewrite.get(fmt_id, set())

    renditions: dict[ArtworkAssetRef, dict[int, Any]] = {}
    to_read: dict[tuple[str, int], list[tuple[ArtworkAssetRef, Any]]] = {}
    for asset, decision in pictures.items():
        grouped = _classify_existing_entry_formats(decision.existing_entry, required_format_ids, device_format_defs)
        renditions[asset] = {**grouped.required_known, **grouped.extra_known}
        for fmt_id, ref in renditions[asset].items():
            if not stays_in_place(ref, fmt_id):
                to_read.setdefault((ref.path, fmt_id), []).append((asset, ref))

    pixels: dict[tuple[ArtworkAssetRef, int], bytes] = {}
    for (path, fmt_id), wanted in to_read.items():
        try:
            with open(path, "rb") as handle:
                for asset, ref in sorted(wanted, key=lambda pair: pair[1].ithmb_offset):
                    handle.seek(ref.ithmb_offset)
                    data = handle.read(ref.size)
                    if len(data) == ref.size:
                        pixels[(asset, fmt_id)] = data
                    else:
                        logger.debug("ART: short read for preserved %s fmt %s", asset, fmt_id)
        except OSError as exc:
            logger.warning("ART: failed to read preserved ithmb %s: %s", path, exc)

    payloads: dict[ArtworkAssetRef, ArtworkPayload] = {}
    for asset, decision in pictures.items():
        formats: dict[int, Any] = dict(asset_passthrough_format_refs.get(asset, {}) or {})
        for fmt_id, ref in renditions[asset].items():
            data = pixels.get((asset, fmt_id))
            if data is not None:
                formats[fmt_id] = EncodedFormatPayload.from_existing_ref(ref, data)
            elif stays_in_place(ref, fmt_id):
                formats[fmt_id] = PassthroughFormatRef.from_existing_ref(ref)
        if formats:
            payloads[asset] = ArtworkPayload(formats=formats, src_img_size=decision.src_img_size)

    salvaged = dropped = 0
    for asset, decision in pictures.items():
        payload = payloads.get(asset) or ArtworkPayload(formats={}, src_img_size=decision.src_img_size)
        missing = [f for f in asset_target_format_ids.get(asset, sorted(required)) if f not in payload.formats]
        if not missing:
            continue
        source = _salvage_source(decision, device_format_defs)
        if source is None:
            payloads.pop(asset, None)
            dropped += 1
            continue
        rebuilt = _encode_all(source, missing, device_formats, device_format_defs, required, asset, salvage=True)
        payload.formats.update(rebuilt)
        if required <= set(payload.formats):
            payloads[asset] = payload
            salvaged += bool(rebuilt)
        else:
            payloads.pop(asset, None)
            dropped += 1
    return payloads, salvaged, dropped


# ── which shard files get rewritten ─────────────────────────────────


def _ithmb_file_index(filename: str, fmt_id: int) -> int | None:
    match = re.fullmatch(rf"F{int(fmt_id)}_(\d+)\.ithmb", str(filename or ""))
    return int(match.group(1)) if match else None


def _select_ithmb_rewrite_plan(
    existing_art: dict[int, dict], decisions: dict[int, TrackArtworkDecision],
    new_artwork: dict[ArtworkAssetRef, ArtworkPayload],
) -> tuple[dict[int, set[str]], dict[int, int]]:
    """Per format: the first shard with room for the new pixels (rewritten from there on).

    Returns ``({format: {filename to rewrite}}, {format: index before the first rewritten shard})``.
    """
    highest: dict[int, int] = {}
    for entry in existing_art.values():
        for fmt_id, ref in (entry.get("formats") or {}).items():
            index = _ithmb_file_index(_ref_filename(ref, fmt_id), fmt_id)
            if index is not None:
                highest[fmt_id] = max(highest.get(fmt_id, 0), index)

    kept_bytes: Counter = Counter()
    counted: set = set()
    for decision in decisions.values():
        if decision.kind not in _PRESERVE_KINDS or decision.asset_ref is None or decision.asset_ref in counted:
            continue
        counted.add(decision.asset_ref)
        for fmt_id, ref in ((decision.existing_entry or {}).get("formats") or {}).items():
            filename = _ref_filename(ref, fmt_id)
            if _ithmb_file_index(filename, fmt_id) is not None:
                kept_bytes[(fmt_id, filename)] += int(ref.size)

    new_bytes: Counter = Counter()
    largest: dict[int, int] = {}
    for payload in new_artwork.values():
        for fmt_id, item in payload.formats.items():
            if isinstance(item, EncodedFormatPayload):
                new_bytes[fmt_id] += item.size
                largest[fmt_id] = max(largest.get(fmt_id, 0), item.size)

    rewrite: dict[int, set[str]] = {}
    starts: dict[int, int] = {}
    for fmt_id, total in new_bytes.items():
        candidates = range(1, highest.get(fmt_id, 0) + 2)
        chosen = next(
            (index for budget in (total, largest[fmt_id]) for index in candidates
             if kept_bytes[(fmt_id, ithmb_filename(fmt_id, index))] + budget <= ITHMB_MAX_SIZE_BYTES),
            None,
        )
        if chosen is None:
            raise RuntimeError(
                f"Artwork format {fmt_id} has a {largest[fmt_id]}-byte payload, exceeding the "
                f"{ITHMB_MAX_SIZE_BYTES}-byte ITHMB file limit."
            )
        rewrite[fmt_id] = {ithmb_filename(fmt_id, chosen)}
        starts[fmt_id] = chosen - 1
    return rewrite, starts


# ── shard writing ───────────────────────────────────────────────────


class _ShardWriter:
    """Appends payloads to per-format temp ``.ithmb`` shards, rolling over at the size cap."""

    def __init__(self, artwork_dir: str, start_indices: Mapping[int, int], protected: Mapping[int, set[str]],
                 formats: list[int], mutation: Callable[[], None]) -> None:
        self.artwork_dir = artwork_dir
        self.protected = protected
        self.mutation = mutation
        self.slots = {fmt: {"index": start_indices.get(fmt, 0), "offset": 0, "handle": None, "filename": ""} for fmt in formats}
        self.staged: dict[tuple[int, int], tuple[Path, str]] = {}
        self.sizes: dict[str, int] = {}

    def _close(self, slot: dict) -> None:
        handle, slot["handle"] = slot.get("handle"), None
        if handle is None:
            return
        try:
            flush_written_file(handle)
        finally:
            handle.close()

    def _next_file(self, fmt_id: int) -> None:
        slot = self.slots[fmt_id]
        self._close(slot)
        index = slot["index"] + 1
        while ithmb_filename(fmt_id, index) in self.protected.get(fmt_id, set()):
            index += 1  # never overwrite a shard that is referenced in place
        filename = ithmb_filename(fmt_id, index)
        final = os.path.join(self.artwork_dir, filename)
        self.mutation()
        temp, handle = open_unique_sibling_temp(final, mode="wb")
        slot.update(index=index, offset=0, filename=filename, handle=handle)
        self.staged[(fmt_id, index)] = (temp, final)

    def append(self, fmt_id: int, data: bytes) -> IthmbLocation:
        slot = self.slots[fmt_id]
        if slot["handle"] is None or (slot["offset"] > 0 and slot["offset"] + len(data) > ITHMB_MAX_SIZE_BYTES):
            self._next_file(fmt_id)
        where = IthmbLocation(slot["filename"], slot["offset"])
        slot["handle"].write(data)
        slot["offset"] += len(data)
        self.sizes[slot["filename"]] = slot["offset"]
        return where

    def close_all(self) -> None:
        failures: list[BaseException] = []
        for slot in self.slots.values():
            try:
                self._close(slot)
            except BaseException as exc:
                failures.append(exc)
        if failures:
            raise failures[0]

    def renames(self) -> list[tuple[str, str]]:
        return [(str(temp), final) for _key, (temp, final) in sorted(self.staged.items(), key=lambda item: item[0])]


def _progress_for_decisions(summary: ArtworkDecisionSummary) -> str:
    parts = []
    kept = summary.preserved_unchanged + summary.preserved_fallback
    if kept:
        parts.append(f"preserving {kept} existing")
    if summary.reencoded:
        parts.append(f"updating {summary.reencoded} changed/new")
    if summary.cleared:
        parts.append(f"clearing {summary.cleared}")
    return f"Artwork — verifying artwork links ({', '.join(parts)})" if parts else "Artwork — verifying existing artwork links"


_format_artwork_decision_progress = _progress_for_decisions


def _progress_for_write(writable: int, total: int, cleared: int) -> str:
    kept = total - writable
    if writable:
        message = f"Artwork — writing {writable} changed/new image{'s' if writable != 1 else ''}"
        return f"{message}, preserving {kept} existing" if kept else message
    if total:
        return f"Artwork — updating artwork index ({_plural(total, 'existing image')}, no image data rewritten)"
    if cleared:
        return "Artwork — updating artwork index (clearing artwork links)"
    return "Artwork — updating artwork index (no live artwork)"


def _common_sizes(entries: list[ArtworkEntry]) -> dict[int, int]:
    """Most common payload size per format (what the file list advertises)."""
    by_format: dict[int, list[int]] = {}
    for entry in entries:
        for fmt_id, item in entry.formats.items():
            if item.size:
                by_format.setdefault(fmt_id, []).append(int(item.size))
    sizes = {}
    for fmt_id, observed in by_format.items():
        sizes[fmt_id] = Counter(observed).most_common(1)[0][0]
        if len(set(observed)) > 1:
            logger.warning(
                "ART: format %s has mixed payload sizes %s; using most common %d in MHIF",
                fmt_id, sorted(set(observed)), sizes[fmt_id],
            )
    return sizes


# ── entry point ─────────────────────────────────────────────────────


def write_artworkdb(
    ipod_path: str,
    tracks: list,
    pc_file_paths: dict | None = None,
    start_img_id: int = 100,
    reference_artdb_path: str | None = None,
    artwork_formats: dict[int, tuple[int, int]] | None = None,
    defer_commit: bool = False,
    progress_callback: Callable[[str], None] | None = None,
    before_device_mutation: Callable[[], None] | None = None,
) -> dict | PendingArtworkWrite:
    """Rewrite the ArtworkDB and any ``.ithmb`` shards that change.

    Returns ``{db_track_id: (img_id, source image size)}`` after committing, or a
    :class:`PendingArtworkWrite` when *defer_commit* is set.
    """
    def progress(message: str) -> None:
        if progress_callback is not None:
            progress_callback(message)

    def mutation() -> None:
        if before_device_mutation is not None:
            before_device_mutation()

    tracks = list(tracks or [])
    subtree = os.path.join("iPod_Control", "Artwork")
    artwork_dir = str(safe_device_path(ipod_path, subtree, allowed_subtree=subtree))
    if not os.path.isdir(artwork_dir):
        mutation()
        os.makedirs(artwork_dir, exist_ok=True)

    sources: dict[int, str] = {}
    for key, path in (pc_file_paths or {}).items():
        try:
            number = int(key)
        except (TypeError, ValueError):
            continue
        if number > 0:
            sources[number] = str(path)

    device_formats = dict(artwork_formats) if artwork_formats is not None else get_artwork_formats(ipod_path)
    device_format_defs = get_artwork_format_definitions(ipod_path) or {}
    required = sorted(device_formats)
    logger.info("ART: using formats %s", required)
    if not required:
        raise RuntimeError("No artwork format definitions are available for this iPod; cannot write ArtworkDB safely.")

    reference_mhfd = None
    if reference_artdb_path and os.path.exists(reference_artdb_path):
        with open(reference_artdb_path, "rb") as handle:
            reference_mhfd = handle.read()
    artdb_final = os.path.join(artwork_dir, "ArtworkDB")
    existing_art = read_existing_artwork(artdb_final, artwork_dir)
    if existing_art:
        logger.info("ART: read %d existing image entries from ArtworkDB", len(existing_art))

    # 1. decide
    progress(f"Artwork — scanning {len(tracks)} tracks")
    decisions, summary = _collect_track_artwork_decisions(tracks, sources, existing_art)
    progress(_progress_for_decisions(summary))
    logger.info(
        "ART decisions: preserve=%d fallback=%d reencode=%d clear=%d",
        summary.preserved_unchanged, summary.preserved_fallback, summary.reencoded, summary.cleared,
    )

    # 2. produce payloads
    targets, passthrough = _collect_rewrite_targets(decisions, required, device_format_defs)
    new_artwork = _convert_new_pc_art(decisions, required, targets, device_formats, device_format_defs, progress)
    rewrite_files, start_indices = (
        _select_ithmb_rewrite_plan(existing_art, decisions, new_artwork) if new_artwork else ({}, {})
    )
    preserved, summary.salvaged, dropped = _load_preserved_art_payloads(
        decisions, required, targets, passthrough, device_formats, device_format_defs,
        passthrough_known_formats=True, rewrite_known_filenames=rewrite_files,
    )
    summary.dropped_invalid += dropped
    if summary.salvaged > 0:
        logger.info(
            "ART: salvaged %d preserved artwork %s via decode/re-encode fallback",
            summary.salvaged, "entry" if summary.salvaged == 1 else "entries",
        )
    payloads = {**new_artwork, **preserved}

    # 3. one entry per track that keeps art
    entries: list[ArtworkEntry] = []
    entry_assets: list[ArtworkAssetRef] = []
    for track in tracks:
        db_track_id = _int(_field(track, "db_track_id"))
        decision = decisions.get(db_track_id) if db_track_id else None
        if decision is None or decision.kind == ArtworkDecisionKind.CLEAR_ART or decision.asset_ref is None:
            continue
        payload = payloads.get(decision.asset_ref)
        if payload is None:
            summary.dropped_invalid += 1
            continue
        entries.append(ArtworkEntry(
            img_id=start_img_id + len(entries), db_track_id=db_track_id,
            art_hash=str(decision.asset_ref.value) if decision.asset_ref.source == "pc" else None,
            src_img_size=payload.src_img_size, formats=payload.formats, db_track_ids=[db_track_id],
        ))
        entry_assets.append(decision.asset_ref)
    distinct = list(dict.fromkeys(entry_assets))
    logger.info(
        "ART result: %d live entries from %d unique payloads (%d dropped invalid)",
        len(entries), len(distinct), summary.dropped_invalid,
    )
    writable = [a for a in distinct if any(isinstance(p, EncodedFormatPayload) for p in payloads[a].formats.values())]
    progress(_progress_for_write(len(writable), len(distinct), summary.cleared))

    image_sizes = _common_sizes(entries)
    written_formats = sorted({f for e in entries for f, p in e.formats.items() if isinstance(p, EncodedFormatPayload)})
    in_place_only = sorted({f for e in entries for f in e.formats} - set(written_formats))
    if in_place_only:
        logger.info("ART: preserving passthrough-only formats without rewriting files: %s", in_place_only)
    protected: dict[int, set[str]] = {}
    for entry in entries:
        for fmt_id, item in entry.formats.items():
            if isinstance(item, PassthroughFormatRef):
                protected.setdefault(fmt_id, set()).add(_ref_filename(item, fmt_id))

    # 4. stage shards and the index
    shards = _ShardWriter(artwork_dir, start_indices, protected, written_formats, mutation)
    artdb_temp: Path | None = None
    locations_by_image: dict[int, dict[int, IthmbLocation]] = {}
    try:
        try:
            placed: dict[ArtworkAssetRef, dict[int, IthmbLocation]] = {}
            for entry, asset in zip(entries, entry_assets):
                if asset not in placed:
                    placed[asset] = {
                        fmt_id: (
                            shards.append(fmt_id, entry.formats[fmt_id].data)
                            if isinstance(entry.formats[fmt_id], EncodedFormatPayload)
                            else IthmbLocation(_ref_filename(entry.formats[fmt_id], fmt_id), int(entry.formats[fmt_id].ithmb_offset))
                        )
                        for fmt_id in sorted(entry.formats)
                    }
                locations_by_image[entry.img_id] = placed[asset]
        finally:
            shards.close_all()
        index = build_artworkdb(
            entries, locations_by_image, sorted({f for e in entries for f in e.formats}),
            image_sizes, start_img_id + len(entries), reference_mhfd,
        )
        mutation()
        artdb_temp, artdb_file = open_unique_sibling_temp(artdb_final, mode="wb")
        try:
            artdb_file.write(index)
            flush_written_file(artdb_file)
        finally:
            artdb_file.close()
    except BaseException:
        for temp in [temp for temp, _final in shards.staged.values()] + ([artdb_temp] if artdb_temp else []):
            try:
                mutation()
                safe_unlink(temp, missing_ok=True)
            except Exception:
                pass
        raise

    links = {entry.db_track_id: (entry.img_id, entry.src_img_size) for entry in entries}
    renames = shards.renames() + [(str(artdb_temp), artdb_final)]
    if defer_commit:
        logger.info(
            "ART: prepared %d unique images, %d MHII entries (per-track) — commit deferred", len(distinct), len(entries),
        )
        return PendingArtworkWrite(db_track_id_to_art_info=links, _pending_renames=renames)

    # 5. commit
    pending = list(renames)
    try:
        while pending:
            temp, final = pending[0]
            mutation()
            safe_replace(temp, final)
            pending.pop(0)
    except BaseException:
        for temp, _final in pending:
            try:
                mutation()
                safe_unlink(temp, missing_ok=True)
            except Exception:
                pass
        raise
    logger.info("Wrote ithmb files: %d unique images, %d MHII entries (per-track)", len(distinct), len(entries))
    for filename, size in sorted(shards.sizes.items()):
        logger.info("  %s: %d bytes", filename, size)
    for fmt_id in in_place_only:
        logger.info("  F%s_N.ithmb: preserved in place", fmt_id)
    return links
