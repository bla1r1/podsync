"""Virtual iPods: ordinary directories that look and behave like a mounted iPod.

``make_virtual_ipod`` lays out ``iPod_Control``, writes a plausible SysInfo,
HashInfo and an empty iTunesDB, and records the synthesized identity in an
``iPodInfo.json`` marker.  ``load_virtual_ipod_info`` turns that marker back
into an :class:`IpodDevice`, topping gaps up from the capability catalog.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import string
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from podsync.hardware.catalog.capabilities import ModelTraits, traits_for_model
from podsync.hardware.catalog.checksum import CHECKSUM_MHBD_SCHEME, SignatureKind
from podsync.hardware.catalog.models import IPOD_MODELS, IPOD_RECOVERY_USB_PIDS, SERIAL_SUFFIX_TO_MODEL, USB_PID_TO_MODEL
from podsync.hardware.current import IpodDevice
from podsync.hardware.virtual.identity import VIRTUAL_IPOD_INFO_FILENAME

__all__ = [
    "VIRTUAL_IPOD_INFO_FILENAME", "available_virtual_ipod_models", "ensure_virtual_itunes_database",
    "has_virtual_ipod_info", "load_virtual_ipod_info", "make_virtual_ipod", "virtual_ipod_info_path",
]

_SOURCE = "iPodInfo.json"
_SCHEMA_VERSION = 1
_APPLE_VID = 0x05AC
_UNKNOWN_CHECKSUM = int(SignatureKind.UNKNOWN)


def virtual_ipod_info_path(path: str | Path) -> Path:
    """Path of the marker file that makes a directory a virtual iPod."""
    return Path(path) / VIRTUAL_IPOD_INFO_FILENAME


def has_virtual_ipod_info(path: str | Path) -> bool:
    """Whether *path* is a virtual iPod."""
    if not path:
        return False
    try:
        return virtual_ipod_info_path(path).is_file()
    except OSError:
        return False


# ── synthesized identity ────────────────────────────────────────────


def _suffix_for_model(model_number: str) -> str | None:
    """The longest (then alphabetically first) serial suffix that maps to *model_number*."""
    suffixes = sorted((s for s, m in SERIAL_SUFFIX_TO_MODEL.items() if m == model_number), key=lambda s: (-len(s), s))
    return suffixes[0] if suffixes else None


def available_virtual_ipod_models() -> list[dict[str, Any]]:
    """Every catalog model a virtual iPod can be made of (it needs a known serial suffix)."""
    rows = []
    for number, (family, generation, capacity, color) in IPOD_MODELS.items():
        suffix = _suffix_for_model(number)
        if suffix:
            name = " ".join(part for part in (family, generation, capacity, color) if part)
            rows.append({
                "model_number": number, "model_family": family, "generation": generation, "capacity": capacity,
                "color": color, "serial_suffix": suffix, "display_name": f"{name} ({number})",
            })
    return sorted(rows, key=lambda r: (r["model_family"], r["generation"], r["capacity"], r["color"], r["model_number"]))


def _firmware_for(family: str, generation: str) -> str:
    if family == "iPod Classic":
        return "2.0.5"
    if family == "iPod Nano" and generation in {"5th Gen", "6th Gen", "7th Gen"}:
        return "1.0.4"
    if family == "iPod" and generation in {"5th Gen", "5.5th Gen"}:
        return "1.3"
    return "1.0"


def _board_for(family: str, generation: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", f"{family} {generation}") or "iPod"


_FAMILY_IDS = (("shuffle", 6), ("nano", 10), ("classic", 11), ("mini", 8))


def _family_id_for(family: str) -> int:
    lowered = family.casefold()
    return next((fid for word, fid in _FAMILY_IDS if word in lowered), 1)


def _usb_pid_for_identity(family: str, generation: str) -> int:
    """A normal-mode USB product id for the model: exact generation first, then a family-wide id."""
    normal = [(pid, fam, gen) for pid, (fam, gen) in USB_PID_TO_MODEL.items() if pid not in IPOD_RECOVERY_USB_PIDS]
    for wanted in (generation, ""):
        for pid, fam, gen in normal:
            if fam == family and gen == wanted:
                return pid
    return 0


def _random_guid() -> str:
    while set(guid := secrets.token_hex(8).upper()) == {"0"}:
        pass
    return guid


def _random_serial(suffix: str) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8)) + suffix


def _formats_json(formats) -> dict[str, list[int]]:
    return {str(fmt.format_id): [fmt.width, fmt.height] for fmt in formats}


def _marker_payload(root: Path, model: str, row, suffix: str, traits: ModelTraits | None, ipod_name: str,
                    iv: bytes, rndpart: bytes) -> dict[str, Any]:
    family, generation, capacity, color = row
    checksum = traits.checksum if traits is not None else SignatureKind.NONE
    guid = _random_guid()
    firmware = _firmware_for(family, generation)
    family_id = _family_id_for(family)
    return {
        "schema_version": _SCHEMA_VERSION,
        "created_by": "podsync",
        "created_at": datetime.now(UTC).isoformat(),
        "ipod_name": (ipod_name or "").strip() or "iPod",
        "mount_name": root.name or "iPod",
        "model_number": model,
        "model_family": family,
        "generation": generation,
        "capacity": capacity,
        "color": color,
        "serial": _random_serial(suffix),
        "serial_suffix": suffix,
        "firewire_guid": guid,
        "firmware": firmware,
        "board": _board_for(family, generation),
        "family_id": family_id,
        "updater_family_id": family_id,
        "product_type": model,
        "usb_vid": _APPLE_VID,
        "usb_pid": _usb_pid_for_identity(family, generation),
        "usb_serial": guid,
        "connected_bus": "USB",
        "reported_volume_format": "FAT32",
        "filesystem_type": "fat32",
        "scsi_vendor": "Apple",
        "scsi_product": "iPod",
        "scsi_revision": firmware,
        "checksum_type": int(checksum),
        "hashing_scheme": CHECKSUM_MHBD_SCHEME.get(checksum, 0),
        "hash_info_iv": iv.hex().upper(),
        "hash_info_rndpart": rndpart.hex().upper(),
        "db_version": traits.db_version if traits else 0,
        "shadow_db_version": traits.shadow_db_version if traits else 0,
        "uses_sqlite_db": traits.uses_sqlite_db if traits else False,
        "supports_sparse_artwork": traits.supports_sparse_artwork if traits else False,
        "podcasts_supported": traits.supports_podcast if traits else True,
        "voice_memos_supported": False,
        "artwork_formats": _formats_json(traits.cover_art_formats if traits and traits.supports_artwork else ()),
        "photo_formats": _formats_json(traits.photo_formats if traits else ()),
        "chapter_image_formats": {},
    }


def _sysinfo_text(payload: dict[str, Any]) -> str:
    def hex_word(value: int) -> str:
        return f"0x{value:08X}" if value else ""

    lines = (
        ("ModelNumStr", payload["model_number"]), ("FirewireGuid", payload["firewire_guid"]),
        ("pszSerialNumber", payload["serial"]), ("BoardHwName", payload["board"]),
        ("visibleBuildID", payload["firmware"]), ("ModelFamily", payload["model_family"]),
        ("Generation", payload["generation"]), ("Capacity", payload["capacity"]), ("Color", payload["color"]),
        ("USBProductID", hex_word(payload["usb_pid"])), ("FamilyID", hex_word(payload["family_id"])),
        ("UpdaterFamilyID", hex_word(payload["family_id"])),
    )
    return "".join(f"{key}: {value}\n" for key, value in lines if value)


def make_virtual_ipod(ipod_path: str | Path, model_number: str, *, ipod_name: str = "iPod") -> IpodDevice:
    """Create (or refresh) a virtual iPod of *model_number* at *ipod_path* and return it."""
    root = Path(os.path.expanduser(str(ipod_path))).resolve()
    model = str(model_number or "").strip().upper()
    if not model:
        raise ValueError("Choose an iPod model")
    row = IPOD_MODELS.get(model)
    if row is None:
        raise ValueError(f"Unknown iPod model: {model}")
    suffix = _suffix_for_model(model)
    if not suffix:
        raise ValueError(f"No known serial suffix for model {model}")
    traits = traits_for_model(row[0], row[1], capacity=row[2], model_number=model)

    control = root / "iPod_Control"
    for name in ("Device", "iTunes", "Music", "Artwork"):
        (control / name).mkdir(parents=True, exist_ok=True)
    if traits is not None and traits.uses_sqlite_db:
        (control / "iTunes" / "iTunes Library.itlp").mkdir(parents=True, exist_ok=True)

    iv, rndpart = secrets.token_bytes(16), secrets.token_bytes(12)
    payload = _marker_payload(root, model, row, suffix, traits, ipod_name, iv, rndpart)
    virtual_ipod_info_path(root).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (control / "Device" / "SysInfo").write_text(_sysinfo_text(payload), encoding="utf-8")
    try:
        from podsync.itdb.writer.signing.aes72 import write_hash_info

        uuid = bytes.fromhex(payload["firewire_guid"])[:20].ljust(20, b"\x00")
        write_hash_info(str(root), uuid, iv, rndpart)
    except Exception:  # HashInfo only matters for nano 5G-style signing
        pass

    ensure_virtual_itunes_database(root)
    return load_virtual_ipod_info(root)


# ── loading ─────────────────────────────────────────────────────────

_TEXT_FIELDS = (
    "model_number", "model_family", "generation", "capacity", "color", "firewire_guid", "serial", "firmware", "board",
    "product_type", "connected_bus", "reported_volume_format", "filesystem_type", "scsi_vendor", "scsi_product",
    "scsi_revision",
)
_INT_FIELDS = (
    "family_id", "updater_family_id", "usb_pid", "usb_vid", "db_version", "shadow_db_version", "checksum_type",
    "hashing_scheme",
)
_FLAG_FIELDS = ("uses_sqlite_db", "supports_sparse_artwork", "podcasts_supported", "voice_memos_supported")
_HASH_INFO_FIELDS = (("hash_info_iv", 16), ("hash_info_rndpart", 12))


def _int_value(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip(), 0)
    except (TypeError, ValueError):
        return 0


def _format_map(value: Any) -> dict[int, tuple[int, int]]:
    formats: dict[int, tuple[int, int]] = {}
    for key, dims in (value.items() if isinstance(value, dict) else ()):
        try:
            formats[int(key)] = (int(dims[0]), int(dims[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return formats


def _fixed_hex(text: Any, size: int) -> bytes:
    try:
        raw = bytes.fromhex(str(text or ""))
    except ValueError:
        return b""
    return raw if len(raw) == size else b""


def _read_marker(path: str | Path) -> dict[str, Any]:
    marker = virtual_ipod_info_path(path)
    if not marker.is_file():
        raise FileNotFoundError(f"Virtual iPod metadata not found at {marker}")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Invalid virtual iPod metadata at {marker}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid virtual iPod metadata at {marker}")
    return payload


def _pin_volume_identity(info: IpodDevice, root: Path) -> None:
    try:
        from podsync.hardware.safety.fsprofile import profile_volume
        from podsync.hardware.safety.readiness import lock_key_for
        from podsync.hardware.virtual.identity import virtual_ipod_profile

        profile = virtual_ipod_profile(profile_volume(str(root)), str(root))
        if profile.identity.is_complete:
            info.volume_identity_key = lock_key_for(profile)
    except OSError:
        pass


def _fill_from_catalog(info: IpodDevice) -> None:
    traits = traits_for_model(info.model_family, info.generation, capacity=info.capacity or None,
                              model_number=info.model_number or None)
    if traits is None:
        return
    info.db_version = info.db_version or traits.db_version
    if int(info.checksum_type) == _UNKNOWN_CHECKSUM:
        info.checksum_type = int(traits.checksum)
    if not info.artwork_formats and traits.supports_artwork:
        info.artwork_formats = {f.format_id: (f.width, f.height) for f in traits.cover_art_formats}
    if not info.photo_formats:
        info.photo_formats = {f.format_id: (f.width, f.height) for f in traits.photo_formats}
    info.shadow_db_version = info.shadow_db_version or traits.shadow_db_version
    info.uses_sqlite_db = bool(info.uses_sqlite_db or traits.uses_sqlite_db)
    info.supports_sparse_artwork = bool(info.supports_sparse_artwork or traits.supports_sparse_artwork)
    info.podcasts_supported = bool(info.podcasts_supported or traits.supports_podcast)


def load_virtual_ipod_info(path: str | Path) -> IpodDevice:
    """The :class:`IpodDevice` recorded in a virtual iPod's marker file."""
    payload = _read_marker(path)
    root = Path(os.path.realpath(path))
    info = IpodDevice(path=str(root), mount_name=str(payload.get("mount_name") or root.name or "iPod"))
    info.ipod_name = str(payload.get("ipod_name") or "")

    for name in _TEXT_FIELDS:
        value = payload.get(name)
        if isinstance(value, str) and value:
            setattr(info, name, value)
            info._field_sources[name] = _SOURCE
    if not info.reported_volume_format and isinstance(payload.get("volume_format"), str):
        info.reported_volume_format = payload["volume_format"]  # pre-release marker spelling
    for name in _INT_FIELDS:
        if name in payload:
            setattr(info, name, _int_value(payload[name]))
    if "checksum_type" not in payload:
        info.checksum_type = _UNKNOWN_CHECKSUM
    _pin_volume_identity(info, root)
    for name in _FLAG_FIELDS:
        if name in payload:
            setattr(info, name, bool(payload[name]))
            info._field_sources[name] = _SOURCE
    info.usb_serial = str(payload.get("usb_serial") or info.firewire_guid or "")
    for name, size in _HASH_INFO_FIELDS:
        setattr(info, name, _fixed_hex(payload.get(name), size))
    info.artwork_formats = _format_map(payload.get("artwork_formats"))
    info.photo_formats = _format_map(payload.get("photo_formats"))
    info.chapter_image_formats = _format_map(payload.get("chapter_image_formats"))

    _fill_from_catalog(info)
    try:
        usage = shutil.disk_usage(root)
        info.disk_size_gb = round(usage.total / 1e9, 1)
        info.free_space_gb = round(usage.free / 1e9, 1)
    except OSError:
        pass
    info.identification_method = "filesystem"
    return info


def ensure_virtual_itunes_database(path: str | Path) -> str | None:
    """Path of the virtual iPod's database, creating an empty one when missing."""
    from podsync.hardware.current import locate_database

    existing = locate_database(str(path))
    if existing:
        return existing
    from podsync.hardware.bootstrap import ensure_device_itunes_database

    return ensure_device_itunes_database(str(path), load_virtual_ipod_info(path))
