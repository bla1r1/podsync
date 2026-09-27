"""Completing an :class:`IpodDevice` from every source the host can see (never writes to the iPod).

Order of evidence, strongest first:

1. the OS hardware probe (skipped when the scanner already ran it);
2. a live SCSI VPD query — the device's own serial and SysInfoExtended;
3. the cached ``SysInfoExtended`` and ``SysInfo`` files on the iPod;
4. on Windows, the GUID in the USBSTOR registry instance names;

then the catalog: model table, serial-suffix lookup, USB PID, generation
from capacity.  Afterwards the identity is made consistent (§10.4 of the
device-identity chapter): catalog spelling, the live USB PID as an anchor,
removal of capacity/color combinations that model never shipped in, and a
unique color when the model came in one colour only.  Finally the signature
kind, its HashInfo material, artwork formats and disk size are filled in.

Conflicting values are resolved by :data:`~podsync.hardware.current.SOURCE_RANK`;
every replacement of a real value is logged at WARNING.
"""

from __future__ import annotations

import logging
import re
import shutil
import struct
import sys
from pathlib import Path
from typing import Any

from podsync.hardware.catalog.checksum import MHBD_SCHEME_TO_CHECKSUM, SignatureKind
from podsync.hardware.catalog.lookup import (
    extract_model_number,
    get_model_info,
    infer_generation,
    lookup_by_serial,
    usb_pid_identity_conflicts,
)
from podsync.hardware.catalog.models import IPOD_MODELS, USB_PID_TO_MODEL, canonicalize_model_identity
from podsync.hardware.catalog.capabilities import checksum_type_for_family_gen
from podsync.hardware.catalog.sysinfo import normalize_guid, parse_sysinfo_extended
from podsync.hardware.current import IpodDevice, locate_database, read_sysinfo, source_rank
from podsync.hardware.safety.guard import UnsafeWriteError

__all__ = ["enrich", "estimate_capacity"]

logger = logging.getLogger(__name__)

_SENTINEL_FAMILY = "iPod"
_UNKNOWN_CHECKSUM = int(SignatureKind.UNKNOWN)
_MODEL_PARTS = ("model_family", "generation", "capacity", "color")
_EXTENDED_INTERNAL = {"model_raw", "sysinfo_extended_raw_xml", "sysinfo_extended_used_regex_fallback"}
# Generation sources a live USB PID may overrule.
_WEAK_GENERATION_SOURCES = {"usb_pid", "sysinfo", "sysinfo_extended", "hashing", "unknown"}
_HASH72_MARKER_OFFSET = 0x72
_ARTWORKDB_SCAN_BYTES = 64 * 1024

_CAPACITY_THRESHOLDS = (
    (140, "160GB"), (100, "120GB"), (65, "80GB"), (50, "60GB"), (35, "40GB"), (25, "30GB"), (17, "20GB"),
    (14, "16GB"), (12, "15GB"), (8.5, "10GB"), (6.5, "8GB"), (5.2, "6GB"), (4.2, "5GB"), (3, "4GB"),
    (1.5, "2GB"), (0.7, "1GB"), (0.3, "512MB"),
)


def estimate_capacity(disk_size_gb: float) -> str:
    """Marketing capacity for a formatted size in GB (first threshold reached)."""
    return next((label for limit, label in _CAPACITY_THRESHOLDS if disk_size_gb >= limit), "")


_estimate_capacity = estimate_capacity


def _is_blank(value: Any) -> bool:
    return value is None or value == "" or value == b""


def _family_unknown(info: IpodDevice) -> bool:
    return info.model_family in ("", _SENTINEL_FAMILY)


def _set_field_from_source(info: IpodDevice, name: str, value: Any, source: str) -> None:
    """The ranked setter: fill empty fields; replace or re-attribute only with an equal-or-better source."""
    if _is_blank(value):
        return
    current = getattr(info, name, None)
    current_source = info._field_sources.get(name)
    is_default = current_source is None and isinstance(current, bool)  # a flag nobody has reported yet
    if is_default or (current in (None, "", b"", 0, {}) and not isinstance(current, bool)):
        setattr(info, name, value)
        info._field_sources[name] = source
        return
    if name == "firewire_guid":
        equal = normalize_guid(current) == normalize_guid(value)
    else:
        equal = str(current).strip() == str(value).strip()
    if source_rank(source) > source_rank(current_source):
        return
    if not equal:
        logger.warning("Replacing %s=%r (%s) with %r (%s)", name, current, current_source, value, source)
        setattr(info, name, value)
    info._field_sources[name] = source


def _fill(info: IpodDevice, name: str, value: Any, source: str) -> None:
    """Set *name* only when it is still empty."""
    if not _is_blank(value) and not getattr(info, name):
        setattr(info, name, value)
        info._field_sources.setdefault(name, source)


def _apply_identity(info: IpodDevice, identity: dict[str, Any], default_source: str) -> None:
    sources = identity.get("_sources", {})
    for name, value in identity.items():
        if not name.startswith("_") and name not in _EXTENDED_INTERNAL and hasattr(info, name):
            _set_field_from_source(info, name, value, sources.get(name, default_source))


# ── 2. evidence from the device and its files ───────────────────────


def _apply_hardware_probe(info: IpodDevice) -> None:
    """Step 2.1, only when the scanner has not already probed this mount."""
    if not info.path or info.raw_identity_evidence.get("hardware"):
        return
    try:
        from podsync.hardware.discovery import scan

        found = scan._probe_hardware(info.path, info.mount_name or info.path)
    except Exception as exc:
        logger.debug("Hardware probe for %s failed: %s", info.path, exc)
        return
    info.raw_identity_evidence["hardware"] = [found]
    sources = found.get("_sources", {})
    if (guid := normalize_guid(found.get("firewire_guid"))) and not info.firewire_guid:
        _fill(info, "firewire_guid", guid, sources.get("firewire_guid", "hardware"))
    for name in ("serial", "firmware", "usb_pid", "model_number"):
        _fill(info, name, found.get(name), sources.get(name, "hardware"))
    for name in ("usb_vid", "usb_serial", "usbstor_instance_id", "usb_parent_instance_id",
                 "usb_grandparent_instance_id", "scsi_vendor", "scsi_product", "scsi_revision"):
        _set_field_from_source(info, name, found.get(name), sources.get(name, "hardware"))
    if found and info.identification_method == "unknown":
        info.identification_method = "hardware"


def _looks_like_live_apple_device(info: IpodDevice) -> bool:
    return bool(info.usb_pid or normalize_guid(info.firewire_guid) or info.scsi_vendor.casefold().startswith("apple"))


def _apply_live_vpd(info: IpodDevice) -> None:
    """Step 2.2: the device's own answer over SCSI; the payload is never cached on the iPod."""
    if not info.path or "live_vpd" in info.raw_identity_evidence or not _looks_like_live_apple_device(info):
        return
    from podsync.hardware.probes import libusb as libusb_probe

    try:
        found = libusb_probe.identify_by_vpd(mount_path=info.path, usb_pid=info.usb_pid,
                                             firewire_guid=info.firewire_guid, write_sysinfo_to_device=False)
    except Exception as exc:
        logger.debug("Live VPD query for %s failed: %s", info.path, exc)
        found = None
    info.raw_identity_evidence["live_vpd"] = [found] if found else []
    if not found:
        return
    source = str(found.get("source") or "vpd")
    for name in ("serial", "firewire_guid", "firmware"):
        _set_field_from_source(info, name, found.get(name), source)
    payload = found.get("vpd_info") or {}
    _set_field_from_source(info, "board", payload.get("BoardHwName"), source)
    if found.get("model_number"):
        for name in ("model_number", "model_family", "generation", "capacity", "color"):
            if found.get(name):
                setattr(info, name, found[name])
                info._field_sources[name] = source
    if raw := payload.get("vpd_raw_xml"):
        _apply_identity(info, parse_sysinfo_extended(raw, source=source, live=True).identity, source)
    if (new_path := found.get("mount_path")) and new_path != info.path:
        logger.info("Live VPD identification moved the iPod mount: %s -> %s", info.path, new_path)
        info.path = new_path
    if info.serial:
        info.identification_method = "usb_vpd"
    logger.info("Live VPD for %s: serial=%s model=%s via %s", info.path, found.get("serial"),
                found.get("model_number"), payload.get("_transport", source))


def _apply_sysinfo_extended(info: IpodDevice) -> None:
    extended = Path(info.path) / "iPod_Control" / "Device" / "SysInfoExtended"
    try:
        if extended.is_file():
            _apply_identity(info, parse_sysinfo_extended(extended.read_bytes()).identity, "sysinfo_extended")
    except OSError as exc:
        logger.debug("SysInfoExtended unavailable: %s", exc)


def _apply_sysinfo(info: IpodDevice) -> None:
    sysinfo = info.sysinfo or {}
    if not sysinfo:
        return
    _fill(info, "board", sysinfo.get("BoardHwName"), "sysinfo")
    serial = str(sysinfo.get("pszSerialNumber") or "").strip()
    if serial and not info.serial:
        if normalize_guid(serial) and normalize_guid(serial) == normalize_guid(info.firewire_guid):
            logger.warning("Ignoring SysInfo serial that equals the FireWire GUID")
        else:
            _fill(info, "serial", serial, "sysinfo")
    for key in ("visibleBuildID", "VisibleBuildID", "BuildID"):
        _fill(info, "firmware", sysinfo.get(key), "sysinfo")
    _fill(info, "firewire_guid", normalize_guid(sysinfo.get("FirewireGuid")), "sysinfo")
    if sysinfo.get("ModelNumStr"):
        _fill(info, "model_number", extract_model_number(sysinfo["ModelNumStr"]), "sysinfo")
    pid_family_with_generation = info._field_sources.get("model_family") == "usb_pid" and info.generation
    if sysinfo.get("ModelFamily") and _family_unknown(info) and not pid_family_with_generation:
        info.model_family = sysinfo["ModelFamily"]
        info._field_sources["model_family"] = "sysinfo"
    for key, name in (("Generation", "generation"), ("Capacity", "capacity"), ("Color", "color")):
        _fill(info, name, sysinfo.get(key), "sysinfo")
    if not info.usb_pid and sysinfo.get("USBProductID"):
        try:
            _fill(info, "usb_pid", int(str(sysinfo["USBProductID"]).strip(), 0), "sysinfo")
        except ValueError:
            pass
    for key, name in (("FamilyID", "family_id"), ("UpdaterFamilyID", "updater_family_id")):
        if sysinfo.get(key):
            try:
                _set_field_from_source(info, name, int(str(sysinfo[key]).strip(), 0), "sysinfo")
            except ValueError:
                pass


def _registry_guid(serial: str) -> str:
    """Step 2.5: a GUID from ``Enum\\USBSTOR`` Apple iPod instance names (Windows only)."""
    try:
        import winreg
    except ImportError:
        return ""
    wanted = serial.upper()
    first = ""
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Enum\USBSTOR")
    except OSError:
        return ""
    with root:
        for index in range(4096):
            try:
                name = winreg.EnumKey(root, index)
            except OSError:
                break
            if "APPLE" not in name.upper() or "IPOD" not in name.upper():
                continue
            try:
                with winreg.OpenKey(root, name) as device_key:
                    instances = [winreg.EnumKey(device_key, i) for i in range(winreg.QueryInfoKey(device_key)[0])]
            except OSError:
                continue
            for instance in instances:
                guid = next((normalize_guid(seg) for seg in instance.split("&")
                             if re.fullmatch(r"[0-9A-Fa-f]{16}", seg) and normalize_guid(seg)), "")
                if not guid:
                    continue
                if wanted and wanted in instance.upper():
                    return guid
                first = first or guid
    if first and wanted:
        logger.warning("No USBSTOR instance mentions serial %s; using GUID %s, which may be stale", serial, first)
    return first


# ── 3–6. catalog lookups ────────────────────────────────────────────


def _apply_model_table(info: IpodDevice) -> None:
    if not info.model_number or not _family_unknown(info):
        return
    row = get_model_info(info.model_number)
    if row is None:
        return
    source = info._field_sources.get("model_number", "model_table")
    info.model_family, info.generation = row[0], row[1]
    info._field_sources.update(model_family=source, generation=source)
    for name, value in (("capacity", row[2]), ("color", row[3])):
        if not getattr(info, name):
            setattr(info, name, value)
            info._field_sources[name] = source


def _apply_serial_lookup(info: IpodDevice) -> None:
    if len(info.serial or "") < 3 or (hit := lookup_by_serial(info.serial)) is None:
        return
    model, row = hit
    serial_source = info._field_sources.get("serial", "serial_lookup")
    for name, value in zip(("model_number", *_MODEL_PARTS), (model, *row)):
        if source_rank(serial_source) <= source_rank(info._field_sources.get(name)) or not getattr(info, name):
            if name == "model_number" and info.model_number and info.model_number != model:
                logger.warning("Serial lookup replaces model number %s with %s", info.model_number, model)
            setattr(info, name, value)
            info._field_sources[name] = serial_source
    if info.identification_method in ("unknown", "hardware"):
        info.identification_method = "serial"


def _apply_usb_pid(info: IpodDevice) -> None:
    if not _family_unknown(info) or info.usb_pid not in USB_PID_TO_MODEL:
        return
    family, generation = USB_PID_TO_MODEL[info.usb_pid]
    info.model_family = family
    info._field_sources["model_family"] = "usb_pid"
    if not info.generation and generation:
        info.generation = generation
        info._field_sources["generation"] = "usb_pid"


def _apply_generation_from_capacity(info: IpodDevice) -> None:
    if info.model_family and not info.generation:
        if generation := infer_generation(info.model_family, info.capacity or estimate_capacity(info.disk_size_gb)):
            info.generation = generation
            info._field_sources["generation"] = "inferred"


# ── 7 / 14. consistency ─────────────────────────────────────────────


def _set_if_different(info: IpodDevice, name: str, value: str, source: str) -> None:
    if value and getattr(info, name) != value:
        setattr(info, name, value)
        info._field_sources[name] = source


def _canonicalize_info(info: IpodDevice) -> None:
    """Catalog spelling: from the model row when known, else normalised family/generation/color."""
    row = IPOD_MODELS.get(info.model_number) if info.model_number else None
    if row is not None:
        source = info._field_sources.get("model_number", "model_table")
        for name, value in zip(_MODEL_PARTS, row):
            _set_if_different(info, name, value, source)
        return
    family, generation, color = canonicalize_model_identity(info.model_family, info.generation, color=info.color)
    for name, value in (("model_family", family), ("generation", generation), ("color", color)):
        _set_if_different(info, name, value, "model_table")


def _anchor_to_usb_pid(info: IpodDevice) -> None:
    """A live USB PID outranks cached model claims that contradict it."""
    if info.usb_pid not in USB_PID_TO_MODEL:
        return
    pid_family, pid_generation = USB_PID_TO_MODEL[info.usb_pid]
    row = IPOD_MODELS.get(info.model_number) if info.model_number else None
    if row is not None and usb_pid_identity_conflicts(row[0], row[1], pid_family, pid_generation):
        logger.warning("Model %s conflicts with USB PID 0x%04X (%s %s); clearing it", info.model_number,
                       info.usb_pid, pid_family, pid_generation)
        info.model_number = ""
        info._field_sources.pop("model_number", None)
    if pid_family and info.model_family != pid_family:
        logger.warning("Family %s conflicts with USB PID 0x%04X; using %s", info.model_family, info.usb_pid, pid_family)
        info.model_family = pid_family
        info._field_sources["model_family"] = "usb_pid"
    if (pid_generation and info.generation != pid_generation
            and info._field_sources.get("generation", "unknown") in _WEAK_GENERATION_SOURCES):
        logger.warning("Generation %s conflicts with USB PID 0x%04X; using %s", info.generation, info.usb_pid,
                       pid_generation)
        info.generation = pid_generation
        info._field_sources["generation"] = "usb_pid"


def _clear(info: IpodDevice, name: str, reason: str) -> None:
    logger.warning("Clearing %s=%r (%s): %s", name, getattr(info, name), info._field_sources.get(name), reason)
    setattr(info, name, "")
    info._field_sources.pop(name, None)


def _drop_impossible_variants(info: IpodDevice) -> None:
    """Remove a capacity/color the model table never lists for this family and generation."""
    if not info.model_family or not info.generation or not (info.capacity or info.color):
        return
    rows = [row for row in IPOD_MODELS.values() if row[0] == info.model_family and row[1] == info.generation]
    if not rows:
        return
    capacity, color = info.capacity, info.color
    if capacity and color and any(row[2] == capacity and row[3] == color for row in rows):
        return
    capacity_known = not capacity or any(row[2] == capacity for row in rows)
    color_known = not color or any(row[3] == color for row in rows)
    if capacity and not capacity_known:
        _clear(info, "capacity", f"no {info.model_family} {info.generation} has that capacity")
    if color and not color_known:
        _clear(info, "color", f"no {info.model_family} {info.generation} comes in that color")
    if capacity and color and capacity_known and color_known:
        capacity_rank = source_rank(info._field_sources.get("capacity"))
        color_rank = source_rank(info._field_sources.get("color"))
        reason = "that capacity and color never shipped together"
        if capacity_rank >= color_rank:
            _clear(info, "capacity", reason)
        if color_rank >= capacity_rank:
            _clear(info, "color", reason)


def _infer_unique_color(info: IpodDevice) -> None:
    if info.color or not info.model_family or not info.generation:
        return
    colors = {row[3] for row in IPOD_MODELS.values() if row[0] == info.model_family and row[1] == info.generation
              and (not info.capacity or row[2] == info.capacity)}
    if len(colors) == 1 and (color := colors.pop()):
        info.color = color
        info._field_sources["color"] = "model_table"


def _sanitize(info: IpodDevice) -> None:
    _canonicalize_info(info)
    _anchor_to_usb_pid(info)
    _drop_impossible_variants(info)


# ── 8–11. signature, HashInfo, artwork ──────────────────────────────


def _read_database_head(ipod_path: str, size: int) -> bytes:
    try:
        path = locate_database(ipod_path)
        if not path:
            return b""
        with open(path, "rb") as handle:
            return handle.read(size)
    except (OSError, UnsafeWriteError):
        return b""


def _read_hashing_scheme(ipod_path: str) -> int:
    header = _read_database_head(ipod_path, 256)
    if len(header) < 0xA0 or header[:4] != b"mhbd":
        return -1
    return struct.unpack_from("<H", header, 0x30)[0]


def _resolve_checksum(info: IpodDevice) -> int:
    if (info.model_family and info.model_family != _SENTINEL_FAMILY) or info.generation:
        if (kind := checksum_type_for_family_gen(info.model_family, info.generation)) is not None:
            return int(kind)
    if info.path and (Path(info.path) / "iPod_Control" / "Device" / "HashInfo").exists():
        return int(SignatureKind.HASH72)
    scheme = MHBD_SCHEME_TO_CHECKSUM.get(info.hashing_scheme)
    if scheme in (SignatureKind.HASH58, SignatureKind.HASH72):
        return int(scheme)
    firmware_major = re.match(r"\s*(\d+)", str(info.firmware or ""))
    if (firmware_major and int(firmware_major.group(1)) >= 2) or info.firewire_guid:
        return _UNKNOWN_CHECKSUM
    return int(SignatureKind.NONE)


def _load_hash_info(info: IpodDevice) -> None:
    """HashInfo file first; else recover the material from the database's own HASH72 signature."""
    try:
        blob = (Path(info.path) / "iPod_Control" / "Device" / "HashInfo").read_bytes()
        if len(blob) >= 54 and blob[:6] == b"HASHv0":
            info.hash_info_rndpart, info.hash_info_iv = blob[26:38], blob[38:54]
            return
    except OSError:
        pass
    path = locate_database(info.path) if info.path else None
    try:
        data = Path(path).read_bytes() if path else b""
    except OSError:
        return
    if len(data) < 0xA0 or data[:4] != b"mhbd" or data[_HASH72_MARKER_OFFSET:_HASH72_MARKER_OFFSET + 2] != b"\x01\x00":
        return
    from podsync.itdb.writer.signing.aes72 import extract_hash_info_to_dict

    try:
        material = extract_hash_info_to_dict(data)
    except Exception as exc:
        logger.debug("HASH72 material recovery failed for %s: %s", info.path, exc)
        return
    if material:
        info.hash_info_iv, info.hash_info_rndpart = material["iv"], material["rndpart"]
        logger.info("Recovered HASH72 material for %s from the existing database signature", info.path)


def _artwork_formats_from_artworkdb(ipod_path: str) -> dict[int, tuple[int, int]]:
    from podsync.artwork.spec.files import extract_format_ids
    from podsync.hardware.catalog.artwork import ITHMB_FORMAT_MAP

    try:
        with open(Path(ipod_path) / "iPod_Control" / "Artwork" / "ArtworkDB", "rb") as handle:
            head = handle.read(_ARTWORKDB_SCAN_BYTES)
    except OSError:
        return {}
    if head[:4] != b"mhfd":
        return {}
    return {fid: (ITHMB_FORMAT_MAP[fid].width, ITHMB_FORMAT_MAP[fid].height)
            for fid in extract_format_ids(head) if fid in ITHMB_FORMAT_MAP}


def _apply_artwork_formats(info: IpodDevice) -> None:
    if info.artwork_formats or not info.model_family:
        return
    from podsync.hardware.catalog.artwork import ithmb_formats_for_device

    info.artwork_formats = ithmb_formats_for_device(info.model_family, info.generation, capacity=info.capacity or None,
                                                    model_number=info.model_number or None)
    if not info.artwork_formats and info.path:
        info.artwork_formats = _artwork_formats_from_artworkdb(info.path)


# ── the pipeline ────────────────────────────────────────────────────


def enrich(info: IpodDevice) -> IpodDevice:
    """Fill derived identity fields of *info* in place (and return it)."""
    path = info.path
    if path and not info.sysinfo:
        try:
            info.sysinfo = read_sysinfo(path)
        except OSError as exc:
            logger.debug("SysInfo unavailable for %s: %s", path, exc)

    if path:
        _apply_hardware_probe(info)
        _apply_live_vpd(info)
        path = info.path
        _apply_sysinfo_extended(info)
    _apply_sysinfo(info)
    if path and not info.firewire_guid and sys.platform == "win32":
        if guid := _registry_guid(info.serial):
            info.firewire_guid = guid
            info._field_sources.setdefault("firewire_guid", "device_tree")

    _apply_model_table(info)
    _apply_serial_lookup(info)
    _apply_usb_pid(info)
    _apply_generation_from_capacity(info)
    _sanitize(info)

    if info.hashing_scheme == -1 and path:
        info.hashing_scheme = _read_hashing_scheme(path)
    if int(info.checksum_type) == _UNKNOWN_CHECKSUM:
        info.checksum_type = _resolve_checksum(info)
    if path and not (info.hash_info_iv and info.hash_info_rndpart):
        _load_hash_info(info)
    _apply_artwork_formats(info)

    if path and not info.disk_size_gb:
        try:
            usage = shutil.disk_usage(path)
            info.disk_size_gb, info.free_space_gb = round(usage.total / 1e9, 1), round(usage.free / 1e9, 1)
        except OSError:
            pass
    if not info.capacity and info.disk_size_gb and (capacity := estimate_capacity(info.disk_size_gb)):
        info.capacity = capacity
        info._field_sources["capacity"] = "disk_size"

    _sanitize(info)
    _infer_unique_color(info)
    fallback = info._field_sources.get("model_number", info.identification_method)
    for name in (*_MODEL_PARTS, "usb_pid"):
        if getattr(info, name) and name not in info._field_sources:
            info._field_sources[name] = fallback
    return info
