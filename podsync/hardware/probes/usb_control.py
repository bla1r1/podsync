"""SysInfoExtended through Apple's USB vendor control request.

Request type 0xC0, request 0x40, value 2, index = chunk number: each reply is
up to 4 KiB of the XML, and a short reply ends it.  Unlike the SCSI paths this
works without detaching the mass-storage driver, but it needs PyUSB with a
libusb backend (and, on Windows, a WinUSB-bound device, which iPods are not
by default).
"""

from __future__ import annotations

import logging
from typing import Any

from podsync.hardware.catalog.models import IPOD_USB_PIDS
from podsync.hardware.catalog.sysinfo import parse_sysinfo_extended
from podsync.hardware.probes.backend import get_libusb_backend

__all__ = ["APPLE_VID", "query_all_ipod_usb_sysinfo_extended", "query_ipod_usb_sysinfo_extended"]

logger = logging.getLogger(__name__)

APPLE_VID = 0x05AC
_CHUNK = 0x1000
_REQUEST_TYPE, _REQUEST, _VALUE = 0xC0, 0x40, 0x0002
_TIMEOUT_MS = 5000


def _usb_modules():
    try:
        import usb.core  # type: ignore[import-not-found]
        import usb.util  # type: ignore[import-not-found]
    except ImportError:
        logger.info("PyUSB is not installed; USB vendor SysInfoExtended query skipped")
        return None, None
    return usb.core, usb.util


def _find_devices(usb_pid: int = 0) -> list:
    core, _util = _usb_modules()
    if core is None:
        return []
    backend = get_libusb_backend()
    if backend is None:
        logger.info("No libusb backend available; USB vendor SysInfoExtended query skipped")
        return []
    try:
        devices = list(core.find(find_all=True, idVendor=APPLE_VID, backend=backend) or [])
    except Exception as exc:
        logger.info("USB device enumeration failed: %s", exc)
        return []
    return [d for d in devices if d.idProduct in IPOD_USB_PIDS and (not usb_pid or d.idProduct == usb_pid)]


def _device_serial(device) -> str:
    try:
        _core, util = _usb_modules()
        return str(util.get_string(device, device.iSerialNumber) or "").replace(" ", "").upper()
    except Exception:
        return ""


def _read_payload(device) -> bytes:
    chunks: list[bytes] = []
    for index in range(0xFFFF):
        chunk = bytes(device.ctrl_transfer(_REQUEST_TYPE, _REQUEST, _VALUE, index, _CHUNK, timeout=_TIMEOUT_MS))
        chunks.append(chunk)
        if len(chunk) < _CHUNK:
            break
    return b"".join(chunks).rstrip(b"\x00")


def _choose(candidates: list, usb_pid: int, serial_filter: str):
    """The device with the wanted serial; with a PID filter a lone candidate is accepted too."""
    wanted = str(serial_filter or "").replace(" ", "").upper()
    if not wanted:
        return candidates[0]
    match = next((device for device in candidates if _device_serial(device) == wanted), None)
    if match is None and usb_pid and len(candidates) == 1:
        return candidates[0]
    return match


def query_ipod_usb_sysinfo_extended(usb_pid: int = 0, serial_filter: str = "") -> dict[str, Any] | None:
    """Parsed plist keys plus ``usb_pid``, ``usb_serial``, ``vpd_raw_xml`` and transport markers."""
    candidates = _find_devices(usb_pid)
    chosen = _choose(candidates, usb_pid, serial_filter) if candidates else None
    if chosen is None:
        return None
    try:
        payload = _read_payload(chosen)
    except Exception as exc:
        text = str(exc).casefold()
        if "not supported" in text or "not implemented" in text:
            logger.info("The iPod does not support the USB vendor SysInfoExtended request: %s", exc)
        else:
            logger.info("USB vendor SysInfoExtended request failed: %s", exc)
        return None
    if not payload:
        return None
    parsed = parse_sysinfo_extended(payload, source="usb_vendor", live=True)
    if not parsed.plist:
        return None
    return {
        **parsed.plist,
        "usb_pid": int(chosen.idProduct),
        "usb_serial": _device_serial(chosen),
        "vpd_raw_xml": parsed.raw_xml,
        "_source": "usb_vendor",
        "_transport": "usb_vendor_control",
        "_used_usb_vendor": True,
    }


def query_all_ipod_usb_sysinfo_extended() -> list[dict[str, Any]]:
    """The vendor-request SysInfoExtended of every connected iPod."""
    results = []
    for device in _find_devices(0):
        if found := query_ipod_usb_sysinfo_extended(int(device.idProduct), _device_serial(device)):
            results.append(found)
    return results
