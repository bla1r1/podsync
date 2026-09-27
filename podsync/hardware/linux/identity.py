"""Identifying a mounted iPod on Linux without root.

Everything here reads what the kernel and udev already publish:

* sysfs — the USB ancestors of the disk (vendor 05ac, product id, the
  FireWire GUID as USB serial) and the SCSI unit serial the kernel cached
  (``device/serial`` or the raw ``vpd_pg80`` page);
* udev — ``udevadm info`` properties and the ``/run/udev/data`` database,
  including ``ID_PODSYNC_PRODUCT_SERIAL`` published by podsync's rule;
* ``/dev/disk/by-id/ipod-<serial>`` links created by that rule.

Earlier sources win per field.  When no Apple product serial can be found a
WARNING (once per mount) points at the udev integration.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

from podsync.hardware.catalog.models import IPOD_USB_PIDS, USB_PID_TO_MODEL

__all__ = ["find_block_device", "linux_device_identity", "parse_vpd_page_80", "whole_disk_device"]

logger = logging.getLogger(__name__)

_BY_ID_DIRECTORY = Path("/dev/disk/by-id")
_UDEV_DATA_DIRECTORY = Path("/run/udev/data")
_APPLE_VENDOR = "05ac"
_USB_ANCESTOR_DEPTH = 12
_WARNED_MOUNTS: set[str] = set()


# ── block devices ───────────────────────────────────────────────────


def whole_disk_device(device: str) -> str:
    """``/dev/sdb1`` → ``/dev/sdb``; ``mmcblk0p1``/``nvme0n1p2`` lose their ``pN`` suffix."""
    path = os.path.realpath(device) if os.name == "posix" else str(device)
    directory, name = os.path.split(path)
    if match := re.fullmatch(r"(sd[a-z]+)\d+", name):
        name = match.group(1)
    elif match := re.fullmatch(r"((?:mmcblk|nvme)\S*?)p\d+", name):
        name = match.group(1)
    return f"{directory}/{name}" if directory else name


def _decode_octal(text: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def _run_text(args: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _source_from_findmnt(mount_path: str) -> str | None:
    completed = _run_text(["findmnt", "-n", "-o", "SOURCE", "--target", str(mount_path)])
    lines = (completed.stdout or "").splitlines() if completed is not None else []
    if completed is None or completed.returncode != 0 or not lines:
        return None
    source = lines[0].strip()
    return re.sub(r"\[.*\]$", "", source) if source.startswith("/dev/") else None  # drop btrfs "[/subvol]"


def _source_from_proc_mounts(mount_path: str) -> str | None:
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2 and _decode_octal(parts[1]) == str(mount_path) and parts[0].startswith("/dev/"):
                    return parts[0]
    except OSError:
        pass
    return None


def _source_from_lsblk(mount_path: str) -> str | None:
    completed = _run_text(["lsblk", "--json", "--output", "NAME,MOUNTPOINT"])
    try:
        tree = json.loads((completed.stdout if completed is not None else "") or "{}")
    except ValueError:
        return None

    def walk(entries) -> str | None:
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("mountpoint") == str(mount_path):
                return f"/dev/{entry.get('name')}"
            if found := walk(entry.get("children")):
                return found
        return None

    return walk(tree.get("blockdevices") if isinstance(tree, dict) else None)


def find_block_device(mount_path: str) -> str | None:
    """The ``/dev`` node mounted at *mount_path* (findmnt, then /proc/mounts, then lsblk)."""
    return _source_from_findmnt(mount_path) or _source_from_proc_mounts(mount_path) or _source_from_lsblk(mount_path)


# ── value cleaning ──────────────────────────────────────────────────


def _printable_ascii(text: str) -> str:
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        return ""
    return "" if any(ord(char) < 32 or ord(char) == 127 for char in text) else text


def parse_vpd_page_80(data: bytes) -> str:
    """The unit serial from a raw VPD page 0x80 (big-endian length at bytes 2–3)."""
    if len(data) < 4 or data[1] != 0x80:
        return ""
    length = int.from_bytes(data[2:4], "big")
    if length <= 0 or len(data) < 4 + length:
        return ""
    try:
        text = data[4:4 + length].split(b"\x00", 1)[0].decode("ascii").strip()
    except UnicodeDecodeError:
        return ""
    return _printable_ascii(text)


def _clean_product_serial(value: Any) -> str:
    return _printable_ascii(str(value or "").replace("\x00", "").strip())


def _parse_hex_guid(value: Any) -> str:
    text = str(value or "").replace(" ", "")
    return text.upper() if re.fullmatch(r"[0-9A-Fa-f]{16}", text) else ""


def _is_ipod_usb_identity(usb_pid: int | None, product_name: str | None) -> bool:
    if usb_pid is not None and usb_pid in IPOD_USB_PIDS:
        return True
    return str(product_name or "").replace("_", " ").strip().casefold() == "ipod"


# ── sysfs ───────────────────────────────────────────────────────────


def _read_sysfs(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read().strip()


def _read_sysfs_or(path: str, default: str = "") -> str:
    try:
        return _read_sysfs(path)
    except OSError:
        return default


def _is_ipod_scsi_device(base_disk: str) -> bool:
    try:
        vendor = _read_sysfs(f"/sys/block/{base_disk}/device/vendor")
        model = _read_sysfs(f"/sys/block/{base_disk}/device/model")
    except OSError:
        return False
    return vendor.casefold().startswith("apple") and model.casefold().startswith("ipod")


def _cached_product_serial(base_disk: str) -> str:
    """Apple serial the kernel cached from page 0x80: ``device/serial``, else ``device/vpd_pg80``."""
    if serial := _clean_product_serial(_read_sysfs_or(f"/sys/block/{base_disk}/device/serial")):
        return serial
    try:
        with open(f"/sys/block/{base_disk}/device/vpd_pg80", "rb") as handle:
            return parse_vpd_page_80(handle.read())
    except OSError:
        return ""


def _usb_device_identity(device_dir: str) -> tuple[int | None, str, str]:
    """``(product id, product name, serial)`` of a sysfs USB device directory."""
    try:
        pid = int(_read_sysfs(os.path.join(device_dir, "idProduct")), 16)
    except (OSError, ValueError):
        pid = None
    return (pid, _read_sysfs_or(os.path.join(device_dir, "product")),
            _read_sysfs_or(os.path.join(device_dir, "serial")))


def _add_pid(result: dict, sources: dict, pid: int, source: str) -> None:
    result["usb_pid"] = pid
    sources["usb_pid"] = source
    family, generation = USB_PID_TO_MODEL.get(pid, ("", ""))
    if family:
        result.setdefault("model_family", family)
    if generation:
        result.setdefault("generation", generation)


def _take_usb_device(device_dir: str, result: dict, sources: dict) -> bool:
    """Record PID and GUID when *device_dir* is an iPod; ``False`` when it is some other device."""
    pid, product, serial = _usb_device_identity(device_dir)
    if not _is_ipod_usb_identity(pid, product):
        return False
    if pid is not None:
        _add_pid(result, sources, pid, "sysfs")
    if guid := _parse_hex_guid(serial):
        result["firewire_guid"] = guid
        sources["firewire_guid"] = "sysfs"
    return True


def _with_sources(result: dict, sources: dict) -> dict[str, Any]:
    if sources:
        result["_sources"] = sources
    return result


def _identity_from_sysfs(base_disk: str) -> dict[str, Any]:
    """Walk up from the disk to its Apple USB device; add the cached serial for iPods."""
    result: dict[str, Any] = {}
    sources: dict[str, str] = {}
    device_link = f"/sys/block/{base_disk}/device"
    is_ipod_usb = False
    if os.path.exists(device_link):
        node = os.path.realpath(device_link)
        for _ in range(_USB_ANCESTOR_DEPTH):
            node = os.path.dirname(node)
            if not node or node == "/":
                break
            try:
                vendor = _read_sysfs(os.path.join(node, "idVendor"))
            except OSError:
                continue  # not a USB device node yet (interface, host, target …)
            if vendor.lower() == _APPLE_VENDOR:
                is_ipod_usb = _take_usb_device(node, result, sources)
            break
    if is_ipod_usb or _is_ipod_scsi_device(base_disk):
        if serial := _cached_product_serial(base_disk):
            result["serial"] = serial
            sources["serial"] = "sysfs_vpd"
    return _with_sources(result, sources)


def _identity_from_usb_bus(base_disk: str) -> dict[str, Any]:
    """Fallback: the Apple USB device under ``/sys/bus/usb/devices`` that contains the disk."""
    result: dict[str, Any] = {}
    sources: dict[str, str] = {}
    try:
        disk_real = os.path.realpath(f"/sys/block/{base_disk}")
        entries = os.listdir("/sys/bus/usb/devices")
    except OSError:
        return result
    for entry in entries:
        device_dir = os.path.realpath(os.path.join("/sys/bus/usb/devices", entry))
        if not disk_real.startswith(device_dir + os.sep):
            continue
        if _read_sysfs_or(os.path.join(device_dir, "idVendor"), "-").lower() != _APPLE_VENDOR:
            continue
        if _take_usb_device(device_dir, result, sources):
            break
    return _with_sources(result, sources)


# ── udev ────────────────────────────────────────────────────────────


def _udev_database_key(device: str) -> str:
    """``b<major>:<minor>`` for a block device node; ``""`` otherwise."""
    try:
        info = os.stat(device)
        if not stat.S_ISBLK(info.st_mode):
            return ""
        return f"b{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
    except (OSError, AttributeError, ValueError):
        return ""


def _udev_properties(device: str) -> dict[str, str]:
    """``udevadm info`` properties, topped up from the udev database file."""
    properties: dict[str, str] = {}
    completed = _run_text(["udevadm", "info", "--query=property", "--name", device])
    for line in ((completed.stdout or "") if completed is not None else "").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            properties[key.strip()] = value.strip()
    if key := _udev_database_key(device):
        try:
            text = (_UDEV_DATA_DIRECTORY / key).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for line in text.splitlines():
            if line.startswith("E:") and "=" in line:
                name, value = line[2:].split("=", 1)
                properties.setdefault(name.strip(), value.strip())
    return properties


def _identity_from_udev(device: str) -> dict[str, Any]:
    properties = _udev_properties(device)
    result: dict[str, Any] = {}
    sources: dict[str, str] = {}
    if serial := _clean_product_serial(properties.get("ID_PODSYNC_PRODUCT_SERIAL")):
        result["serial"] = serial
        sources["serial"] = "udev_scsi_id"
    elif properties.get("ID_PODSYNC_RULE_VERSION"):
        logger.info("podsync udev rule ran for %s but did not publish an Apple product serial", device)
    if properties.get("ID_VENDOR_ID", "").lower() == _APPLE_VENDOR:
        try:
            pid = int(properties.get("ID_MODEL_ID", ""), 16)
        except ValueError:
            pid = None
        if _is_ipod_usb_identity(pid, properties.get("ID_MODEL")):
            if guid := _parse_hex_guid(properties.get("ID_SERIAL_SHORT")):
                result["firewire_guid"] = guid
                sources["firewire_guid"] = "udev"
            if pid is not None:
                _add_pid(result, sources, pid, "udev")
    return _with_sources(result, sources)


def _identity_from_by_id(whole_disk: str) -> dict[str, Any]:
    """The serial in an ``ipod-<serial>`` link that points at *whole_disk*."""
    try:
        links = sorted(_BY_ID_DIRECTORY.glob("ipod-*"))
    except OSError:
        return {}
    target = os.path.realpath(whole_disk)
    for link in links:
        try:
            if os.path.realpath(str(link)) != target:
                continue
        except OSError:
            continue
        serial = _clean_product_serial(link.name[len("ipod-"):])
        if re.fullmatch(r"[A-Za-z0-9]{8,16}", serial):
            return {"serial": serial, "_sources": {"serial": "udev_scsi_id"}}
    return {}


# ── entry point ─────────────────────────────────────────────────────


def linux_device_identity(mount_path: str) -> dict[str, Any]:
    """Serial, GUID, USB PID (and PID model) of the iPod mounted at *mount_path*."""
    device = find_block_device(mount_path)
    if not device:
        return {}
    disk = whole_disk_device(device)
    base_disk = os.path.basename(disk)
    parts = [_identity_from_sysfs(base_disk), _identity_from_udev(device)]
    if disk != device:
        parts.append(_identity_from_udev(disk))
    parts += [_identity_from_by_id(disk), _identity_from_usb_bus(base_disk)]

    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for part in parts:
        part = part or {}
        part_sources = part.get("_sources", {}) or {}
        for key, value in part.items():
            if key != "_sources" and key not in merged:
                merged[key] = value
                if key in part_sources:
                    sources[key] = part_sources[key]
    _with_sources(merged, sources)

    mount_key = str(mount_path)
    if merged.get("serial"):
        _WARNED_MOUNTS.discard(mount_key)
    elif (merged.get("usb_pid") or merged.get("firewire_guid")) and mount_key not in _WARNED_MOUNTS:
        _WARNED_MOUNTS.add(mount_key)
        logger.warning("The Apple product serial of the iPod at %s is unavailable; set up podsync's Linux "
                       "identity integration to publish it.", mount_path)
    return merged
