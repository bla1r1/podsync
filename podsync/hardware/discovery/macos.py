"""Hardware evidence for a mounted iPod on macOS, from ``diskutil`` and ``ioreg``.

A scan may look at several volumes, so the two ``ioreg`` listings (BSD disk →
USB serial, USB serial → Apple device properties) are taken once per scan and
cached until :func:`clear_usb_cache`.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
import threading
from typing import Any

from podsync.hardware.catalog.models import USB_PID_TO_MODEL

__all__ = ["clear_usb_cache", "parse_ioreg_bsd_serials", "probe_macos_hardware"]

_APPLE_VID = 0x05AC
_SERIAL_LINE = re.compile(r'"USB Serial Number"\s*=\s*"([^"]*)"')
_BSD_LINE = re.compile(r'"BSD Name"\s*=\s*"(disk\d+)"')

_cache_lock = threading.Lock()
_bsd_serials: dict[str, str] | None = None
_usb_devices: dict[str, dict] | None = None


def clear_usb_cache() -> None:
    """Drop the per-scan ioreg listings."""
    global _bsd_serials, _usb_devices
    with _cache_lock:
        _bsd_serials = _usb_devices = None


def _clean_serial(value: Any) -> str:
    return str(value or "").replace(" ", "").upper()


def parse_ioreg_bsd_serials(text: str) -> dict[str, str]:
    """``{bsd disk: usb serial}`` from ``ioreg -r -c IOMedia`` text.

    The most recent USB serial line is remembered; an ``Apple iPod Media``
    node claims it, and the next ``BSD Name`` line binds the disk to it.
    Serials of other devices on the bus never reach a disk this way.
    """
    mapping: dict[str, str] = {}
    current = pending = ""
    for line in str(text or "").splitlines():
        if serial := _SERIAL_LINE.search(line):
            current = _clean_serial(serial.group(1))
        elif "Apple iPod Media" in line:
            pending = current
        elif (bsd := _BSD_LINE.search(line)) and pending:
            mapping[bsd.group(1)] = pending
            pending = ""
    return mapping


def _collect_apple_devices(node: Any, devices: dict[str, dict]) -> None:
    if isinstance(node, list):
        for child in node:
            _collect_apple_devices(child, devices)
    elif isinstance(node, dict):
        if node.get("idVendor") == _APPLE_VID:
            serial = _clean_serial(node.get("USB Serial Number") or node.get("kUSBSerialNumberString"))
            if serial:
                devices[serial] = node
        _collect_apple_devices(node.get("IORegistryEntryChildren", []), devices)


def _run(args: list[str], *, text: bool) -> Any:
    return subprocess.run(args, capture_output=True, text=text, timeout=10, check=False)


def _usb_maps() -> tuple[dict[str, str], dict[str, dict]]:
    global _bsd_serials, _usb_devices
    with _cache_lock:
        if _bsd_serials is None:
            try:
                listing = _run(["ioreg", "-r", "-c", "IOMedia"], text=True).stdout
            except (OSError, subprocess.SubprocessError):
                listing = ""
            _bsd_serials = parse_ioreg_bsd_serials(listing)
        if _usb_devices is None:
            devices: dict[str, dict] = {}
            try:
                raw = _run(["ioreg", "-a", "-r", "-c", "IOUSBHostDevice"], text=False).stdout
                _collect_apple_devices(plistlib.loads(raw) if raw else [], devices)
            except Exception:  # ioreg missing or an unreadable plist: no USB evidence
                pass
            _usb_devices = devices
        return dict(_bsd_serials), dict(_usb_devices)


def probe_macos_hardware(mount_path: str) -> dict[str, Any]:
    """USB PID (and its model), GUID and firmware of the USB disk mounted at *mount_path*."""
    try:
        completed = _run(["diskutil", "info", "-plist", mount_path], text=False)
        info = plistlib.loads(completed.stdout) if completed.returncode == 0 else {}
    except Exception:
        return {}
    if not isinstance(info, dict) or str(info.get("BusProtocol", "")).upper() != "USB":
        return {}
    bsd_serials, devices = _usb_maps()
    device = devices.get(bsd_serials.get(str(info.get("ParentWholeDisk") or ""), ""))
    if device is None and len(devices) == 1:
        device = next(iter(devices.values()))
    if device is None:
        return {}

    result: dict[str, Any] = {}
    pid = device.get("idProduct")
    if isinstance(pid, int):
        result["usb_pid"] = pid
        family, generation = USB_PID_TO_MODEL.get(pid, ("", ""))
        if family:
            result["model_family"] = family
        if generation:
            result["generation"] = generation
    serial = _clean_serial(device.get("USB Serial Number") or device.get("kUSBSerialNumberString"))
    if re.fullmatch(r"[0-9A-F]{16}", serial):
        result["firewire_guid"] = serial
    bcd = device.get("bcdDevice")
    if isinstance(bcd, int):
        result["firmware"] = f"{bcd >> 8}.{bcd & 0xFF:02d}"
    return result
