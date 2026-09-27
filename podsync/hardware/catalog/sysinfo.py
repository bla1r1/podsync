"""Turning the iPod's own self-description into identity fields.

Two sources exist: the ``SysInfo`` text file (``Key: value`` lines) that
iTunes leaves in ``iPod_Control/Device``, and ``SysInfoExtended`` — an XML
plist the device returns over SCSI/USB (and that iTunes may also cache on
disk).  Truncated or slightly broken XML is common, so parsing degrades to a
regex scan rather than failing.
"""

from __future__ import annotations

import plistlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from podsync.hardware.catalog.lookup import extract_model_number

__all__ = [
    "CHAPTER_ART_KEYS", "COVER_ART_KEYS", "DeviceEvidence", "EvidenceValue", "PHOTO_ART_KEYS", "ParsedSysInfoExtended",
    "evidence_from_identity", "extract_image_formats", "identity_from_sysinfo", "identity_from_sysinfo_extended",
    "normalize_guid", "parse_sysinfo_extended", "parse_sysinfo_text",
]

COVER_ART_KEYS = ("AlbumArt", "AlbumArt2", "ArtworkFormats", "CoverArt", "ArtworkCoverArtFormats")
PHOTO_ART_KEYS = ("ImageSpecifications", "PhotoFormats")
CHAPTER_ART_KEYS = ("ChapterImageSpecs", "ChapterImageSpecifications")


def normalize_guid(value: Any) -> str:
    """Upper-case hex FireWire GUID; ``""`` for missing, all-zero or non-hex values."""
    text = str(value if value is not None else "").strip().replace(" ", "")
    if text[:2].lower() == "0x":
        text = text[2:]
    if not text or set(text) <= {"0"} or not re.fullmatch(r"[0-9A-Fa-f]+", text):
        return ""
    return text.upper()


def _number(value: Any) -> int | str:
    """An integer from ``"0x1A"``, ``"26"``, ``"26 (build)"``; the text itself if not numeric."""
    if isinstance(value, (bool, int)):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return 0
    token = text.split()[0]
    for base in (0, 10):
        try:
            return int(token, base)
        except ValueError:
            continue
    return text


def _flag(value: Any) -> bool:
    if isinstance(value, (bool, int, float)):
        return bool(value)
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "on"}


class _Collector:
    """Identity fields plus where each came from."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.fields: dict[str, Any] = {}
        self.sources: dict[str, str] = {}

    def put(self, name: str, value: Any) -> None:
        self.fields[name] = value
        self.sources[name] = self.source

    def model(self, raw: Any) -> None:
        number = extract_model_number(str(raw))
        if number:
            self.put("model_number", number)
        self.fields["model_raw"] = raw

    def done(self) -> dict[str, Any]:
        self.fields["_sources"] = self.sources
        return self.fields


# ── SysInfo (text) ──────────────────────────────────────────────────


def parse_sysinfo_text(content: str) -> dict[str, str]:
    """``Key: value`` pairs of a SysInfo file; comments and blank lines skipped."""
    pairs = {}
    for raw in str(content or "").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and ":" in line:
            key, value = line.split(":", 1)
            pairs[key.strip()] = value.strip()
    return pairs


_SYSINFO_TEXT_FIELDS = (("ModelFamily", "model_family"), ("Generation", "generation"), ("Capacity", "capacity"),
                        ("Color", "color"))


def identity_from_sysinfo(sysinfo: dict[str, str], source: str = "sysinfo") -> dict[str, Any]:
    """Identity fields (with ``_sources``) from parsed SysInfo pairs."""
    out = _Collector(source)
    if sysinfo.get("BoardHwName"):
        out.put("board", sysinfo["BoardHwName"])
    if sysinfo.get("pszSerialNumber"):
        out.put("serial", str(sysinfo["pszSerialNumber"]).strip())
    if guid := normalize_guid(sysinfo.get("FirewireGuid")):
        out.put("firewire_guid", guid)
    firmware = next((sysinfo[k] for k in ("visibleBuildID", "VisibleBuildID", "BuildID") if sysinfo.get(k)), None)
    if firmware:
        out.put("firmware", firmware)
    if sysinfo.get("ModelNumStr"):
        out.model(sysinfo["ModelNumStr"])
    for key, name in _SYSINFO_TEXT_FIELDS:
        if sysinfo.get(key):
            out.put(name, sysinfo[key])
    if sysinfo.get("USBProductID"):
        try:
            out.put("usb_pid", int(str(sysinfo["USBProductID"]).strip(), 0))
        except ValueError:
            pass
    family = sysinfo.get("FamilyID", sysinfo.get("iPodFamily"))
    if family is not None:
        out.put("family_id", _number(family))
    if sysinfo.get("UpdaterFamilyID") is not None:
        out.put("updater_family_id", _number(sysinfo["UpdaterFamilyID"]))
    return out.done()


# ── SysInfoExtended (plist) ─────────────────────────────────────────


def _first_present(entry: dict, *names: str) -> Any:
    return next((entry[name] for name in names if name in entry), None)


def extract_image_formats(plist: dict, keys) -> dict[int, tuple[int, int]]:
    """``{format id: (width, height)}`` from the image-spec arrays under *keys*."""
    formats: dict[int, tuple[int, int]] = {}
    for key in keys:
        entries = plist.get(key)
        for entry in entries if isinstance(entries, list) else ():
            if not isinstance(entry, dict):
                continue
            try:
                fid = int(_first_present(entry, "FormatId", "CorrelationID", "format_id"))
                width = int(_first_present(entry, "RenderWidth", "DisplayWidth", "Width", "width"))
                height = int(_first_present(entry, "RenderHeight", "DisplayHeight", "Height", "height"))
            except (TypeError, ValueError):
                continue
            if fid > 0 and width > 0 and height > 0:
                formats[fid] = (width, height)
    return formats


@dataclass
class ParsedSysInfoExtended:
    """A parsed SysInfoExtended plist plus how it had to be recovered."""

    plist: dict
    raw_xml: bytes = b""
    source: str = "sysinfo_extended"
    live: bool = False
    used_regex_fallback: bool = False
    repaired: bool = False  # parsed only after dropping keys the firmware put inside arrays

    @property
    def identity(self) -> dict[str, Any]:
        return identity_from_sysinfo_extended(self, self.source, live=self.live)

    @property
    def cover_art_formats(self) -> dict[int, tuple[int, int]]:
        return extract_image_formats(self.plist, COVER_ART_KEYS)

    @property
    def photo_formats(self) -> dict[int, tuple[int, int]]:
        return extract_image_formats(self.plist, PHOTO_ART_KEYS)

    @property
    def chapter_image_formats(self) -> dict[int, tuple[int, int]]:
        return extract_image_formats(self.plist, CHAPTER_ART_KEYS)


_SCALAR_PAIR = re.compile(r"<key>([^<]*)</key>\s*(?:<string>([^<]*)</string>|<integer>([^<]*)</integer>|<(true|false)\s*/>)")


def _salvage_scalars(data: bytes) -> dict[str, Any]:
    """Top-level-ish key/value pairs from XML that ``plistlib`` rejected."""
    found: dict[str, Any] = {}
    for key, text, integer, boolean in _SCALAR_PAIR.findall(data.decode("utf-8", errors="replace")):
        if text or (not integer and not boolean):
            found[key] = text
        elif integer:
            try:
                found[key] = int(integer.strip(), 0)
            except ValueError:
                found[key] = integer
        else:
            found[key] = boolean == "true"
    return found


_TAG = re.compile(rb"<(/?)(array|dict|key)\b[^>]*?(/?)>")
_ARRAY_KEY = re.compile(rb"<key>[^<]*</key>\s*")


def _drop_keys_inside_arrays(data: bytes) -> bytes:
    """Remove ``<key>`` elements that sit directly in an ``<array>``.

    iPod 5G/5.5G firmware lists image specs as ``<array><key>1019</key><dict>…``,
    which no plist parser accepts; without the stray keys the array is valid.
    """
    out, stack, pos = bytearray(), [], 0
    for match in _TAG.finditer(data):
        closing, name, self_closing = match.group(1), match.group(2), match.group(3)
        if name == b"key":
            if not closing and stack and stack[-1] == b"array":
                stray = _ARRAY_KEY.match(data, match.start())
                if stray:
                    out += data[pos:match.start()]
                    pos = stray.end()
            continue
        if self_closing:
            continue
        if closing:
            if stack and stack[-1] == name:
                stack.pop()
        else:
            stack.append(name)
    return bytes(out + data[pos:])


def _trim_to_plist(data: bytes) -> bytes:
    data = data.strip(b"\x00\r\n\t ")
    for marker in (b"<?xml", b"<plist"):
        at = data.find(marker)
        if at >= 0:
            return data[at:].rstrip(b"\x00")
    return data.rstrip(b"\x00")


def parse_sysinfo_extended(content: bytes | str, *, source: str = "sysinfo_extended", live: bool = False) -> ParsedSysInfoExtended:
    """Parse the plist, repairing firmware quirks and truncation, else fall back to a regex scan.

    ``raw_xml`` keeps the device's bytes (closed if truncated) even when a repaired
    copy was what parsed, so a cached SysInfoExtended stays byte-faithful.
    """
    raw = content.encode("utf-8") if isinstance(content, str) else bytes(content or b"")
    data = _trim_to_plist(raw)
    closed = (data,) if b"</plist>" in data else (data, data + b"</dict></plist>")
    for original in closed:
        repaired = _drop_keys_inside_arrays(original)
        for attempt in (original,) if repaired == original else (original, repaired):
            try:
                loaded = plistlib.loads(attempt)
            except Exception:
                continue
            if isinstance(loaded, dict):
                return ParsedSysInfoExtended(plist=loaded, raw_xml=original, source=source, live=live,
                                             repaired=attempt is repaired)
    salvaged = _salvage_scalars(data)
    return ParsedSysInfoExtended(plist=salvaged, raw_xml=data, source=source, live=live, used_regex_fallback=bool(salvaged))


# plist key → identity field, per value kind.
_PLIST_NUMBERS = (
    ("FamilyID", "family_id"), ("UpdaterFamilyID", "updater_family_id"), ("DBVersion", "db_version"),
    ("ShadowDBVersion", "shadow_db_version"), ("MaxTracks", "max_tracks"), ("MaxTransferSpeed", "max_transfer_speed"),
    ("usb_pid", "usb_pid"), ("usb_vid", "usb_vid"), ("MaxFileSizeInGB", "max_file_size_gb"),
)
_PLIST_TEXTS = (
    ("ProductType", "product_type"), ("ConnectedBus", "connected_bus"), ("VolumeFormat", "reported_volume_format"),
    ("scsi_vendor", "scsi_vendor"), ("scsi_product", "scsi_product"), ("scsi_revision", "scsi_revision"),
    ("usb_serial", "usb_serial"),
)
_PLIST_FLAGS = (
    ("SQLiteDB", "uses_sqlite_db"), ("SupportsSparseArtwork", "supports_sparse_artwork"),
    ("PodcastsSupported", "podcasts_supported"), ("VoiceMemosSupported", "voice_memos_supported"),
)
_PLIST_DICTS = (("AudioCodecs", "audio_codecs"), ("PowerInformation", "power_information"),
                ("AppleDRMVersion", "apple_drm_version"))
_PLIST_FORMATS = ((COVER_ART_KEYS, "artwork_formats"), (PHOTO_ART_KEYS, "photo_formats"),
                  (CHAPTER_ART_KEYS, "chapter_image_formats"))


def _copy(out: _Collector, plist: dict, table, convert: Callable[[Any], Any], *, present: Callable[[dict, str], bool]) -> None:
    for key, name in table:
        if present(plist, key):
            out.put(name, convert(plist[key]))


def identity_from_sysinfo_extended(parsed_or_plist: ParsedSysInfoExtended | dict, source: str = "sysinfo_extended", *,
                                   live: bool = False) -> dict[str, Any]:
    """Identity fields (with ``_sources``) from a SysInfoExtended plist."""
    parsed = parsed_or_plist if isinstance(parsed_or_plist, ParsedSysInfoExtended) else None
    plist = parsed.plist if parsed is not None else dict(parsed_or_plist or {})
    out = _Collector(source)

    def first(*keys: str) -> Any:
        return next((plist[k] for k in keys if plist.get(k) not in (None, "")), None)

    serial = str(plist.get("SerialNumber") or "").strip()
    if serial and not serial.upper().startswith("RAND"):  # "RAND…" is a placeholder, not a serial
        out.put("serial", serial)
    if guid := normalize_guid(first("FireWireGUID", "FirewireGuid", "FireWireGuid")):
        out.put("firewire_guid", guid)
    if (firmware := first("FireWireVersion", "scsi_revision", "VisibleBuildID", "BuildID", "visibleBuildID")) is not None:
        out.put("firmware", str(firmware))
    if (board := first("BoardHwName", "BoardHwID")) is not None:
        out.put("board", str(board))
    if plist.get("ModelNumStr"):
        out.model(plist["ModelNumStr"])

    has_value = lambda p, k: p.get(k) not in (None, "")  # noqa: E731
    _copy(out, plist, _PLIST_NUMBERS, _number, present=has_value)
    _copy(out, plist, _PLIST_TEXTS, str, present=has_value)
    _copy(out, plist, _PLIST_FLAGS, _flag, present=lambda p, k: k in p)
    _copy(out, plist, _PLIST_DICTS, lambda v: v, present=lambda p, k: isinstance(p.get(k), dict))
    for keys, name in _PLIST_FORMATS:
        if formats := extract_image_formats(plist, keys):
            out.put(name, formats)

    if parsed is not None:
        if parsed.raw_xml:
            out.fields["sysinfo_extended_raw_xml"] = parsed.raw_xml
        out.fields["sysinfo_extended_used_regex_fallback"] = parsed.used_regex_fallback
    return out.done()


# ── evidence bundles ────────────────────────────────────────────────


@dataclass(frozen=True)
class EvidenceValue:
    """One identity fact and where it came from."""

    value: Any
    source: str
    live: bool = False
    raw_key: str = ""


@dataclass
class DeviceEvidence:
    """Identity facts with provenance; the first value for a field wins unless replaced."""

    fields: dict[str, EvidenceValue] = field(default_factory=dict)
    blobs: dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, value: Any, source: str, *, live: bool = False, raw_key: str = "",
            replace: bool = False) -> None:
        if value in (None, "", b"") or (name in self.fields and not replace):
            return
        self.fields[name] = EvidenceValue(value, source, live, raw_key)

    def as_flat_dict(self) -> dict[str, Any]:
        return {**{n: ev.value for n, ev in self.fields.items()}, "_sources": {n: ev.source for n, ev in self.fields.items()}}


def evidence_from_identity(identity: dict, *, source: str, live: bool = False) -> DeviceEvidence:
    """A :class:`DeviceEvidence` built from an identity dict."""
    evidence = DeviceEvidence()
    sources = identity.get("_sources", {}) or {}
    for key, value in identity.items():
        if not key.startswith("_") and key not in {"model_raw", "sysinfo_extended_raw_xml"}:
            evidence.add(key, value, sources.get(key, source), live=live, replace=True)
    return evidence
