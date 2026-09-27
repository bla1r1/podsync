"""Finding mounted iPods and working out which model each one is.

Per mount: live hardware evidence (per-OS probe), filesystem evidence
(SysInfo, SysInfoExtended, database header, volume identity), a resolution
step (:mod:`.resolve`), an optional live SCSI VPD query when the model is
still unknown, the master-playlist title, and finally ``enrich``.  Several
mounts of one device (aliases, bind mounts) collapse to one result.

Module-level names used by the pipeline (``sys``, ``_probe_hardware``,
``_identify_via_sysinfo`` …) are looked up at call time so tests can replace
them.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import string
import struct
import sys
from pathlib import Path
from typing import Any

from podsync.hardware.catalog.checksum import MHBD_SCHEME_TO_CHECKSUM, SignatureKind
from podsync.hardware.catalog.models import IPOD_MODELS
from podsync.hardware.catalog.sysinfo import identity_from_sysinfo, normalize_guid, parse_sysinfo_extended, parse_sysinfo_text
from podsync.hardware.current import IpodDevice, locate_database
from podsync.hardware.discovery import macos as macos_discovery
from podsync.hardware.discovery import windows as windows_discovery
from podsync.hardware.discovery.master_title import read_master_playlist_title
from podsync.hardware.enrich import enrich
from podsync.hardware.discovery.resolve import EXTRA_FIELDS, is_empty, resolve_model
from podsync.hardware.linux.identity import linux_device_identity
from podsync.hardware.safety.fsprofile import profile_volume
from podsync.hardware.safety.fstype import detect_volume_format, filesystem_itunesdb_platform
from podsync.hardware.safety.readiness import lock_key_for

__all__ = ["find_ipods", "identify_mounted_ipod"]

logger = logging.getLogger(__name__)

_SYSINFO_EXTENDED_INTERNAL = {"model_raw", "sysinfo_extended_raw_xml", "sysinfo_extended_used_regex_fallback"}


def _clear_macos_usb_cache() -> None:
    macos_discovery.clear_usb_cache()


# ── candidate volumes ───────────────────────────────────────────────


def _has_ipod_control(root: str) -> bool:
    try:
        return os.path.isdir(os.path.join(root, "iPod_Control"))
    except OSError:
        return False


_PSEUDO_FILESYSTEMS = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "overlay", "squashfs",
                       "securityfs", "debugfs", "tracefs", "configfs", "fusectl", "mqueue", "hugetlbfs", "pstore",
                       "bpf", "autofs", "binfmt_misc", "9p", "rpc_pipefs", "nsfs"}


def _mounted_roots() -> list[str]:
    """Mount points of real filesystems, so iPods mounted elsewhere (or for another user) are found."""
    try:
        with open("/proc/self/mounts", encoding="utf-8", errors="replace") as handle:
            entries = [line.split() for line in handle]
    except OSError:
        return []
    return [re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), parts[1]) for parts in entries
            if len(parts) >= 3 and parts[2] not in _PSEUDO_FILESYSTEMS and parts[1] != "/"]


def _linux_candidate_roots() -> list[str]:
    users = [name for name in (os.environ.get("USER") or os.environ.get("LOGNAME"), os.environ.get("SUDO_USER"))
             if name]
    parents = [f"{base}/{user}" for user in dict.fromkeys(users) for base in ("/run/media", "/media")]
    roots: list[str] = []
    for parent in [*parents, "/mnt", "/media"]:
        try:
            roots.extend(os.path.join(parent, name) for name in sorted(os.listdir(parent)))
        except OSError:
            continue
    return roots + _mounted_roots()  # desktop automount places first, then anything else that is mounted


def _find_ipod_volumes() -> list[tuple[str, str]]:
    """``(mount path, display name)`` of every volume with an ``iPod_Control`` directory."""
    found: list[tuple[str, str]] = []
    if sys.platform == "win32":
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            try:
                if os.path.exists(root) and _has_ipod_control(root):
                    found.append((root, f"{letter}:"))
            except OSError:
                continue
    elif sys.platform == "darwin":
        try:
            names = sorted(os.listdir("/Volumes"))
        except OSError:
            names = []
        for name in names:
            root = os.path.join("/Volumes", name)
            if os.path.isdir(root) and _has_ipod_control(root):
                found.append((root, name))
    else:
        seen: set[str] = set()
        for root in _linux_candidate_roots():
            try:
                real = os.path.realpath(root)
            except OSError:
                continue
            if real not in seen and os.path.isdir(root) and _has_ipod_control(root):
                seen.add(real)
                found.append((root, os.path.basename(root)))
    logger.debug("iPod volume candidates on %s: %s", sys.platform, found)
    return found


# ── hardware evidence ───────────────────────────────────────────────


def _probe_hardware(mount_path: str, mount_name: str) -> dict[str, Any]:
    """Live identity from the OS; sources default to the probe method."""
    if sys.platform == "win32":
        result, method = windows_discovery.probe_windows_hardware(mount_name)
    elif sys.platform == "darwin":
        result, method = macos_discovery.probe_macos_hardware(mount_path), "ioreg"
    else:
        result, method = linux_device_identity(mount_path), "linux_identity"
    result = dict(result or {})
    sources = dict(result.get("_sources") or {})
    tree_source = "device_tree" if sys.platform == "win32" else method
    for name, default in (("firewire_guid", tree_source), ("usb_pid", tree_source), ("serial", method),
                          ("firmware", method)):
        if result.get(name):
            sources.setdefault(name, default)
    if sources:
        result["_sources"] = sources
    return result


# ── filesystem evidence ─────────────────────────────────────────────


def _identify_via_sysinfo(ipod_path: str) -> dict[str, Any]:
    """Identity from the cached SysInfoExtended (preferred) and SysInfo files."""
    result: dict[str, Any] = {}
    sources: dict[str, str] = {}
    device_dir = Path(ipod_path) / "iPod_Control" / "Device"

    extended = device_dir / "SysInfoExtended"
    try:
        if extended.is_file():
            parsed = parse_sysinfo_extended(extended.read_bytes())
            identity = parsed.identity
            for key, value in identity.items():
                if not key.startswith("_") and key not in _SYSINFO_EXTENDED_INTERNAL:
                    result[key] = value
                    sources[key] = identity.get("_sources", {}).get(key, "sysinfo_extended")
            result.update(_sysinfo_extended_present=True, _sysinfo_extended_keys=len(parsed.plist),
                          _sysinfo_extended_regex_fallback=parsed.used_regex_fallback)
    except Exception as exc:
        logger.info("Could not parse SysInfoExtended at %s: %s", extended, exc)

    plain = device_dir / "SysInfo"
    try:
        if plain.is_file():
            pairs = parse_sysinfo_text(plain.read_bytes().decode("utf-8", errors="ignore"))
            for key, value in identity_from_sysinfo(pairs, "sysinfo").items():
                if not key.startswith("_") and key != "model_raw" and key not in result:
                    result[key] = value
                    sources[key] = "sysinfo"
            row = IPOD_MODELS.get(result.get("model_number") or "")
            for name, value in zip(("model_family", "generation", "capacity", "color"), row or ()):
                if name not in result and value:
                    result[name] = value
                    sources[name] = sources.get("model_number", "sysinfo")
            result.update(_sysinfo_present=True, _sysinfo_keys=len(pairs))
    except Exception as exc:
        logger.info("Could not parse SysInfo at %s: %s", plain, exc)

    if not sources and not any(not key.startswith("_") for key in result):
        return {}
    result["_sources"] = sources
    return result


# The signature scheme narrows the model down to a class of devices.
_SCHEME_CLASSES = {
    SignatureKind.NONE: ("iPod", "(pre-2007)"),
    SignatureKind.HASH58: ("iPod", "(Classic or Nano 3G/4G)"),
    SignatureKind.HASH72: ("iPod Nano", "(5th gen)"),
    SignatureKind.HASHAB: ("iPod Nano", "(6th/7th gen)"),
}


def _identify_via_hashing_scheme(ipod_path: str) -> dict[str, Any]:
    try:
        path = locate_database(ipod_path)
        if not path:
            return {}
        with open(path, "rb") as handle:
            header = handle.read(0x72)
    except Exception:
        return {}
    if len(header) < 0x32 or header[:4] != b"mhbd":
        return {}
    scheme = struct.unpack_from("<H", header, 0x30)[0]
    family, generation = _SCHEME_CLASSES.get(MHBD_SCHEME_TO_CHECKSUM.get(scheme, SignatureKind.NONE),
                                             _SCHEME_CLASSES[SignatureKind.NONE])
    return {"hashing_scheme": scheme, "hash_model_family": family, "hash_generation": generation,
            "_sources": {"hashing_scheme": "itunes"}}


def _merge_evidence(result: dict[str, Any], sources: dict[str, str], found: dict[str, Any]) -> None:
    result.update((key, value) for key, value in found.items() if key != "_sources")
    sources.update(found.get("_sources", {}))


def _probe_filesystem(ipod_path: str) -> dict[str, Any]:
    """Volume identity, filesystem type, SysInfo files and database header of a mount."""
    result: dict[str, Any] = {}
    sources: dict[str, str] = {}
    filesystem_type = detect_volume_format(ipod_path)
    try:
        profile = profile_volume(ipod_path)
        if profile.identity.is_complete:
            result["volume_identity_key"] = lock_key_for(profile)
            sources["volume_identity_key"] = "mounted_volume_identity"
            logger.info("Captured scan-time iPod volume identity: mount=%s", ipod_path)
        filesystem_type = filesystem_type or getattr(profile, "filesystem_type", "") or ""
    except Exception as exc:
        logger.warning("Could not capture scan-time iPod volume identity: mount=%s error=%s", ipod_path, exc)
    if filesystem_type:
        logger.info("iPod mounted filesystem detected: mount=%s filesystem=%s", ipod_path, filesystem_type)
        if sys.platform.startswith("linux") and filesystem_itunesdb_platform(filesystem_type) == 1:
            logger.warning(
                "Mac-formatted iPod filesystem detected on Linux: mount=%s filesystem=%s. Linux "
                "may mount journaled HFS+ read-only; verify write support before syncing.",
                ipod_path, filesystem_type,
            )
    _merge_evidence(result, sources, _identify_via_sysinfo(ipod_path) or {})
    _merge_evidence(result, sources, _identify_via_hashing_scheme(ipod_path) or {})
    if filesystem_type:
        result["filesystem_type"] = filesystem_type
        sources["filesystem_type"] = "mounted_filesystem"
    result["_sources"] = sources
    return result


# ── per-mount identification ────────────────────────────────────────


def _apply_resolution(info: IpodDevice, resolved: dict[str, Any]) -> None:
    info.model_number = resolved.get("model_number", "") or ""
    info.model_family = resolved.get("model_family", "iPod") or "iPod"
    for name in ("generation", "capacity", "color", "firewire_guid", "serial", "firmware"):
        setattr(info, name, resolved.get(name, "") or "")
    info.usb_pid = int(resolved.get("usb_pid") or 0)
    info.hashing_scheme = int(resolved.get("hashing_scheme", -1))
    info.identification_method = resolved.get("identification_method", "filesystem") or "filesystem"
    info.identity_conflicts = list(resolved.get("_conflicts", []))
    for name in EXTRA_FIELDS:
        if not is_empty(resolved.get(name)):
            setattr(info, name, resolved[name])
    info._field_sources.update(resolved.get("_sources", {}))


def _apply_live_vpd(info: IpodDevice) -> None:
    """Ask the device itself (SCSI VPD) when nothing on disk named the model.

    Runs only with a known USB PID, i.e. when a live probe already confirmed
    an Apple device.  On Windows this uses SCSI pass-through, which needs no
    privileges; SysInfo is never written from here.
    """
    from podsync.hardware.probes import libusb as libusb_probe

    try:
        found = libusb_probe.identify_by_vpd(mount_path=info.path, usb_pid=info.usb_pid,
                                             firewire_guid=info.firewire_guid, write_sysinfo_to_device=False)
    except Exception as exc:
        logger.info("Live VPD identification failed for %s: %s", info.path, exc)
        found = None
    info.raw_identity_evidence["live_vpd"] = [found] if found else []  # enrich will not ask again
    if not found:
        return
    if found.get("model_number"):
        for name in ("model_number", "model_family", "generation", "capacity", "color"):
            setattr(info, name, found.get(name) or "")
            info._field_sources[name] = "vpd"
        info.identification_method = "usb_vpd"
    for name in ("serial", "firewire_guid", "firmware"):
        if found.get(name) and not getattr(info, name):
            setattr(info, name, found[name])
            info._field_sources[name] = "vpd"
    new_path = found.get("mount_path")
    if new_path and new_path != info.path:
        logger.info("Live VPD identification moved the iPod mount: %s -> %s", info.path, new_path)
        info.path = new_path


def _identify_ipod_mount(mount_path: str, display_name: str) -> IpodDevice | None:
    from podsync.hardware.virtual.device import has_virtual_ipod_info, load_virtual_ipod_info

    if has_virtual_ipod_info(mount_path):
        try:
            return load_virtual_ipod_info(mount_path)
        except Exception as exc:
            logger.warning("Virtual iPod metadata could not be loaded: %s", exc)
    info = IpodDevice(path=mount_path, mount_name=display_name)
    try:
        usage = shutil.disk_usage(mount_path)
        info.disk_size_gb, info.free_space_gb = usage.total / 1024**3, usage.free / 1024**3
    except OSError:
        info.disk_size_gb = info.free_space_gb = 0.0

    hardware = _probe_hardware(mount_path, display_name)
    filesystem = _probe_filesystem(mount_path)
    _apply_resolution(info, resolve_model(hardware, filesystem, info.disk_size_gb))
    info.raw_identity_evidence = {"hardware": [hardware], "filesystem": [filesystem]}
    if not info.model_number and info.usb_pid:
        _apply_live_vpd(info)
    info.ipod_name = read_master_playlist_title(mount_path)
    enrich(info)
    logger.info("iPod identified: mount=%s model=%s family=%s generation=%s method=%s", mount_path,
                info.model_number, info.model_family, info.generation, info.identification_method)
    return info


def _device_key(device: IpodDevice) -> tuple[str, str] | None:
    """What makes two mounts the same device: GUID, then Apple serial, then real path."""
    for attr in ("firewire_guid", "usb_serial"):
        if guid := normalize_guid(getattr(device, attr, "")):
            return ("guid", guid)
    if serial := str(getattr(device, "serial", "") or "").strip().upper():
        return ("serial", serial)
    if path := str(getattr(device, "path", "") or ""):
        return ("path", os.path.realpath(path))
    return None


def _collapse_aliases(devices: list[IpodDevice]) -> list[IpodDevice]:
    kept: list[IpodDevice] = []
    seen: dict[tuple[str, str], IpodDevice] = {}
    for device in devices:
        key = _device_key(device)
        if key is not None and key in seen:
            logger.debug("Skipping duplicate iPod mount alias: kept=%s skipped=%s key=%s", seen[key].path,
                         device.path, key)
            continue
        if key is not None:
            seen[key] = device
        kept.append(device)
    if len(kept) != len(devices):
        logger.info("Deduplicated iPod scan results: before=%d after=%d", len(devices), len(kept))
    return kept


def find_ipods() -> list[IpodDevice]:
    """Every mounted iPod (real or virtual), one entry per physical device."""
    logger.info("iPod scan started")
    try:
        identified = [_identify_ipod_mount(path, name) for path, name in _find_ipod_volumes()]
        kept = _collapse_aliases([device for device in identified if device is not None])
    finally:
        _clear_macos_usb_cache()
    logger.info("iPod scan finished: count=%d mounts=%s", len(kept),
                ", ".join(device.mount_name for device in kept) or "none")
    return kept


def _normalize_root(ipod_path: str) -> str:
    path = os.path.expanduser(str(ipod_path))
    if sys.platform == "win32" and re.fullmatch(r"[A-Za-z]:\.?", path):
        return path[:2] + "\\"
    return os.path.abspath(path)


def identify_mounted_ipod(ipod_path: str, mount_name: str | None = None) -> IpodDevice | None:
    """Identify the iPod whose root is *ipod_path* (``"D:"`` works on Windows); ``None`` if it is not one."""
    if not ipod_path:
        return None
    path = _normalize_root(ipod_path)
    from podsync.hardware.virtual.device import (
        ensure_virtual_itunes_database,
        has_virtual_ipod_info,
        load_virtual_ipod_info,
    )

    if has_virtual_ipod_info(path):
        try:
            ensure_virtual_itunes_database(path)
            return load_virtual_ipod_info(path)
        except Exception as exc:
            logger.warning("Virtual iPod metadata could not be loaded: %s", exc)
    if not os.path.isdir(os.path.join(path, "iPod_Control")):
        logger.info("Selected path is not an iPod root: %s", path)
        return None
    if mount_name is None:
        if sys.platform == "win32":
            mount_name = os.path.splitdrive(path)[0] or os.path.basename(path.rstrip("\\/"))
        else:
            mount_name = os.path.basename(path.rstrip("/")) or path
    try:
        return _identify_ipod_mount(path, mount_name)
    finally:
        _clear_macos_usb_cache()
