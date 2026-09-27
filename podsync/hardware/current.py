"""The device record, the process-wide "selected iPod", and per-device file decisions.

* :class:`IpodDevice` — everything known about one mounted iPod, with the
  source of each identity field in ``_field_sources``.
* The selected device — writers look up the device for a path here, so a
  database is only ever signed for the iPod the user actually picked.
* Database filename choice (``iTunesDB`` vs ``iTunesCDB``), signature kind
  detection and FireWire id lookup, each preferring the selected device and
  falling back to what the iPod's own files say.

Completing a record from all available sources lives in :mod:`.enrich`.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import sys
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from podsync.hardware.catalog.capabilities import ModelTraits, checksum_type_for_family_gen, traits_for_model
from podsync.hardware.catalog.checksum import SignatureKind
from podsync.hardware.catalog.lookup import extract_model_number, get_model_info
from podsync.hardware.catalog.sysinfo import parse_sysinfo_text
from podsync.hardware.safety.guard import UnsafeWriteError

__all__ = [
    "SOURCE_RANK", "IpodDevice", "UnidentifiedDeviceError", "clear_current_device", "database_filename_for_write",
    "detect_signature_kind", "enrich", "get_firewire_id", "has_exact_model_number", "locate_database",
    "read_sysinfo", "require_exact_model_number", "select_device", "selected_device", "selected_device_at",
]

logger = logging.getLogger(__name__)

_SENTINEL_FAMILY = "iPod"  # the default family: also a real family name, so "iPod" alone proves little
_UNKNOWN_CHECKSUM = int(SignatureKind.UNKNOWN)

# Lower rank = more trustworthy.  Live answers from the device beat OS
# descriptions, which beat catalog lookups, which beat files on the disk.
SOURCE_RANK: dict[str, int] = {
    name: rank for rank, name in enumerate((
        "scsi_vpd", "windows_scsi", "linux_scsi", "sysfs_vpd", "udev_scsi_id", "usb_vendor",
        "vpd", "iokit", "ioctl", "device_tree", "ioreg", "sysfs", "udev", "wmi", "itunes",
        "serial_lookup", "usb_pid", "disk_size", "model_table", "inferred",
        "sysinfo_extended", "sysinfo", "hashing", "unknown",
    ))
}


def source_rank(source: str | None) -> int:
    """Rank of *source*; unlisted names rank after ``unknown``."""
    return SOURCE_RANK.get(str(source or "unknown"), len(SOURCE_RANK))


class UnidentifiedDeviceError(ValueError):
    """No exact model number was resolved for the selected iPod."""


def _guid_bytes(value: Any) -> bytes | None:
    """Bytes of a hex GUID (``0x`` allowed); ``None`` for empty, malformed or all-zero values."""
    text = str(value or "").strip()
    if text[:2].lower() == "0x":
        text = text[2:]
    try:
        raw = bytes.fromhex(text) if text else b""
    except ValueError:
        return None
    return raw if any(raw) else None


@dataclass
class IpodDevice:
    """Everything known about one mounted iPod; ``_field_sources`` names each field's source."""

    path: str = ""
    mount_name: str = ""
    ipod_name: str = ""
    model_number: str = ""
    model_family: str = _SENTINEL_FAMILY
    generation: str = ""
    capacity: str = ""
    color: str = ""
    firewire_guid: str = ""
    serial: str = ""
    firmware: str = ""
    board: str = ""
    family_id: int | str = 0
    updater_family_id: int | str = 0
    product_type: str = ""
    usb_pid: int = 0
    usb_vid: int = 0
    usb_serial: str = ""
    usbstor_instance_id: str = ""
    usb_parent_instance_id: str = ""
    usb_grandparent_instance_id: str = ""
    scsi_vendor: str = ""
    scsi_product: str = ""
    scsi_revision: str = ""
    connected_bus: str = ""
    reported_volume_format: str = ""
    filesystem_type: str = ""
    volume_identity_key: str = ""
    db_version: int = 0
    shadow_db_version: int = 0
    uses_sqlite_db: bool = False
    supports_sparse_artwork: bool = False
    max_tracks: int = 0
    max_transfer_speed: int = 0
    max_file_size_gb: int | float = 0
    podcasts_supported: bool = False
    voice_memos_supported: bool = False
    audio_codecs: dict = field(default_factory=dict)
    power_information: dict = field(default_factory=dict)
    apple_drm_version: dict = field(default_factory=dict)
    checksum_type: int = _UNKNOWN_CHECKSUM
    hashing_scheme: int = -1
    hash_info_iv: bytes = b""
    hash_info_rndpart: bytes = b""
    disk_size_gb: float = 0.0
    free_space_gb: float = 0.0
    artwork_formats: dict = field(default_factory=dict)
    photo_formats: dict = field(default_factory=dict)
    chapter_image_formats: dict = field(default_factory=dict)
    sysinfo: dict = field(default_factory=dict)
    raw_identity_evidence: dict = field(default_factory=dict)
    identity_conflicts: list = field(default_factory=list)
    identification_method: str = "unknown"
    _field_sources: dict = field(default_factory=dict, init=False, repr=False)

    @property
    def firewire_id_bytes(self) -> bytes | None:
        return _guid_bytes(self.firewire_guid)

    @property
    def drive_letter(self) -> str:
        if sys.platform == "win32" and self.path and self.path[0].isalpha():
            return self.path[0]
        return ""

    @property
    def volume_format(self) -> str:
        return self.reported_volume_format

    @volume_format.setter
    def volume_format(self, value: str) -> None:
        self.reported_volume_format = value

    @property
    def display_name(self) -> str:
        return " ".join(part for part in (self.model_family, self.generation, self.capacity, self.color) if part)

    @property
    def subtitle(self) -> str:
        parts = [self.mount_name] if self.mount_name else []
        if self.disk_size_gb:
            parts.append(f"{self.free_space_gb:.1f} of {self.disk_size_gb:.1f} GB free")
        return " — ".join(parts)

    @property
    def icon(self) -> str:
        family, generation = str(self.model_family or "").casefold(), str(self.generation or "").casefold()
        if "classic" in family or (family == "ipod" and generation in {
                "4th gen (photo)", "4th gen (color)", "5th gen", "5.5th gen"}):
            return "\U0001F4F1"  # screen-first players
        if "shuffle" in family:
            return "\U0001F500"
        if "mini" in family:
            return "\U0001F3B6"
        return "\U0001F3B5"

    @property
    def capabilities(self) -> ModelTraits:
        """Catalog traits, overridden by what the device itself reported."""
        traits = traits_for_model(self.model_family, self.generation, capacity=self.capacity or None,
                                  model_number=self.model_number or None) or ModelTraits()
        changes: dict[str, Any] = {}
        if self.db_version:
            changes["db_version"] = int(self.db_version)
        if self.shadow_db_version:
            changes["shadow_db_version"] = int(self.shadow_db_version)
        for name, trait in (("uses_sqlite_db", "uses_sqlite_db"), ("supports_sparse_artwork", "supports_sparse_artwork"),
                            ("podcasts_supported", "supports_podcast")):
            if name in self._field_sources:
                changes[trait] = bool(getattr(self, name))
        return replace(traits, **changes) if changes else traits


# ── the selected device ─────────────────────────────────────────────

_CURRENT_DEVICE: IpodDevice | None = None
_CURRENT_LOCK = threading.Lock()


def has_exact_model_number(info: Any) -> bool:
    """Whether *info* carries a non-empty model number."""
    value = getattr(info, "model_number", None)
    return isinstance(value, str) and bool(value.strip())


def require_exact_model_number(info: Any) -> None:
    """Raise :class:`UnidentifiedDeviceError` unless the model number is known."""
    if not has_exact_model_number(info):
        path = str(getattr(info, "path", "") or "") or "unknown mount"
        raise UnidentifiedDeviceError(f"Refusing to activate unidentified iPod at {path}: no exact model number "
                                      "was resolved")


def selected_device() -> IpodDevice | None:
    """The selected iPod, or ``None``."""
    with _CURRENT_LOCK:
        return _CURRENT_DEVICE


def select_device(info: IpodDevice | None) -> None:
    """Make *info* the selected iPod (``None`` clears); only exactly identified models are accepted."""
    global _CURRENT_DEVICE
    if info is not None:
        require_exact_model_number(info)
    with _CURRENT_LOCK:
        _CURRENT_DEVICE = info
    if info is None:
        logger.info("Device cleared")
        return
    serial = str(getattr(info, "serial", "") or "")
    logger.info("Device set: family=%s generation=%s model=%s serial=...%s guid=%s checksum=%s method=%s "
                "capacity=%s artwork=%s", info.model_family, info.generation, info.model_number, serial[-4:],
                info.firewire_guid, info.checksum_type, info.identification_method, info.capacity,
                sorted(info.artwork_formats or {}))


def clear_current_device() -> None:
    """Forget the selected iPod."""
    select_device(None)


def _same_location(left: str, right: str) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def selected_device_at(path: str) -> IpodDevice | None:
    """The selected device if it is mounted at *path*, else ``None``."""
    try:
        device = selected_device()
        device_path = str(getattr(device, "path", "") or "") if device is not None else ""
        return device if device_path and _same_location(device_path, str(path)) else None
    except Exception:
        return None


def _device_capabilities(device: Any) -> ModelTraits | None:
    traits = getattr(device, "capabilities", None)
    if isinstance(traits, ModelTraits):
        return traits
    return traits_for_model(getattr(device, "model_family", "") or "", getattr(device, "generation", "") or "",
                            capacity=getattr(device, "capacity", None) or None,
                            model_number=getattr(device, "model_number", None) or None)


def _matching_device_capabilities(ipod_path: str) -> ModelTraits | None:
    try:
        device = selected_device()
        device_path = str(getattr(device, "path", "") or "") if device is not None else ""
        if not device_path or not _same_location(device_path, str(ipod_path)):
            return None
        return _device_capabilities(device)
    except Exception:
        return None


# ── database filenames ──────────────────────────────────────────────


def _itunes_dir(ipod_path: str) -> Path:
    return Path(ipod_path) / "iPod_Control" / "iTunes"


def _non_empty_file(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and info.st_size > 0


def _locate_for_known_model(itunes_dir: Path, traits: ModelTraits) -> str | None:
    """The model's own filename, else the other one as a recovery source (logged)."""
    required = "iTunesCDB" if traits.supports_compressed_db else "iTunesDB"
    alternate = "iTunesDB" if required == "iTunesCDB" else "iTunesCDB"
    existing: list[Path] = []
    try:
        for name in (required, alternate):
            candidate = itunes_dir / name
            try:
                info = candidate.stat()
            except FileNotFoundError:
                continue
            existing.append(candidate)
            if _non_empty_file(info):
                if name == alternate:
                    logger.warning("Using %s as the recovery source until the next guarded write restores %s",
                                   candidate, required)
                return str(candidate)
    except OSError as exc:
        raise UnsafeWriteError(f"Could not safely inspect the iPod database filenames: {exc}") from exc
    return str(existing[0]) if existing else None


def locate_database(ipod_path: str) -> str | None:
    """Path of the iPod's database: a non-empty file first, then any existing one, else ``None``."""
    itunes_dir = _itunes_dir(ipod_path)
    traits = _matching_device_capabilities(ipod_path)
    if traits is not None:
        return _locate_for_known_model(itunes_dir, traits)
    candidates = [itunes_dir / "iTunesCDB", itunes_dir / "iTunesDB"]
    for candidate in candidates:
        try:
            if _non_empty_file(candidate.stat()):
                return str(candidate)
        except OSError:
            continue
    return next((str(candidate) for candidate in candidates if candidate.exists()), None)


def database_filename_for_write(ipod_path: str) -> str:
    """``iTunesCDB`` for models that read compressed databases, else ``iTunesDB``."""
    traits = _matching_device_capabilities(ipod_path)
    if traits is not None:
        return "iTunesCDB" if traits.supports_compressed_db else "iTunesDB"
    resolved = locate_database(ipod_path)
    if resolved:
        try:
            if Path(resolved).stat().st_size > 0:
                return os.path.basename(resolved)
        except OSError:
            pass
    return "iTunesDB"


def read_sysinfo(ipod_path: str) -> dict[str, str]:
    """``Key: value`` pairs of the iPod's SysInfo file (``FileNotFoundError`` when absent)."""
    path = Path(ipod_path) / "iPod_Control" / "Device" / "SysInfo"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise FileNotFoundError(f"SysInfo not found at {path}") from None
    return parse_sysinfo_text(raw.decode("utf-8", errors="ignore"))


# ── signatures and FireWire ids ─────────────────────────────────────


def _load_virtual(ipod_path: str):
    from podsync.hardware.virtual.device import has_virtual_ipod_info, load_virtual_ipod_info

    return load_virtual_ipod_info(ipod_path) if has_virtual_ipod_info(ipod_path) else None


def detect_signature_kind(ipod_path: str) -> SignatureKind:
    """Signature the iPod at *ipod_path* expects: selected device, virtual marker, SysInfo model, HashInfo."""
    device = selected_device_at(ipod_path)
    if device is not None:
        value = int(getattr(device, "checksum_type", _UNKNOWN_CHECKSUM) or 0)
        if value != _UNKNOWN_CHECKSUM:
            return SignatureKind(value)
    try:
        virtual = _load_virtual(ipod_path)
        if virtual is not None and int(virtual.checksum_type) != _UNKNOWN_CHECKSUM:
            return SignatureKind(int(virtual.checksum_type))
    except Exception:
        pass
    try:
        sysinfo = read_sysinfo(ipod_path)
    except FileNotFoundError:
        return SignatureKind.UNKNOWN
    model = extract_model_number(sysinfo.get("ModelNumStr", ""))
    row = get_model_info(model) if model else None
    if row is not None and (kind := checksum_type_for_family_gen(row[0], row[1])) is not None:
        return kind
    try:
        if (Path(ipod_path) / "iPod_Control" / "Device" / "HashInfo").exists():
            return SignatureKind.HASH72
    except OSError as exc:
        raise UnsafeWriteError(f"Could not inspect the iPod HashInfo checksum material: {exc}") from exc
    return SignatureKind.UNKNOWN


_EXTENDED_GUID = re.compile(r"<key>FireWireGUID</key>\s*<string>([0-9A-Fa-fx]+)</string>")


def get_firewire_id(ipod_path: str, *, known_guid: str | None = None) -> bytes:
    """FireWire id bytes for signing: caller, selected device, virtual marker, SysInfo, SysInfoExtended."""
    if raw := _guid_bytes(known_guid):
        return raw
    device = selected_device_at(ipod_path)
    if device is not None and (raw := getattr(device, "firewire_id_bytes", None)):
        return raw
    try:
        virtual = _load_virtual(ipod_path)
        if virtual is not None and virtual.firewire_id_bytes:
            return virtual.firewire_id_bytes
    except Exception:
        pass
    try:
        if raw := _guid_bytes(read_sysinfo(ipod_path).get("FirewireGuid")):
            return raw
    except OSError:
        pass
    try:
        text = (Path(ipod_path) / "iPod_Control" / "Device" / "SysInfoExtended").read_text(encoding="utf-8",
                                                                                          errors="ignore")
        if (match := _EXTENDED_GUID.search(text)) and (raw := _guid_bytes(match.group(1))):
            return raw
    except OSError:
        pass
    raise RuntimeError("Could not determine the iPod FireWire ID from the caller, the selected device, "
                       "virtual iPod metadata, SysInfo, or SysInfoExtended. Connect the iPod and try again.")


def enrich(info: IpodDevice) -> IpodDevice:
    """Complete *info* from every host-visible source; see :func:`podsync.hardware.enrich.enrich`."""
    from podsync.hardware.enrich import enrich as _enrich

    return _enrich(info)
