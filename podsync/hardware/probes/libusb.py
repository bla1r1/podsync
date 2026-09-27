"""Live identification over VPD, the PyUSB bulk-only transport, and the SysInfo CLI.

:func:`identify_by_vpd` is the entry point the scanner uses: it collects a
live SysInfoExtended from the best transport the platform offers —

* macOS: IOKit SCSI tasks (:mod:`.macos`);
* Windows: SCSI pass-through on the drive letter (:mod:`.windows`);
* Linux: SG_IO on the block device (:mod:`.linux`);
* as root outside Windows: raw bulk-only mass-storage commands over PyUSB;

optionally merged with Apple's USB vendor control request (:mod:`.usb_control`),
and maps the Apple serial to a model.  ``python -m podsync.hardware.probes.libusb``
prints what every connected iPod reports and can write SysInfo files.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import re
import struct
import subprocess
import sys
import time
from typing import Any

from podsync.hardware.catalog.lookup import lookup_by_serial
from podsync.hardware.catalog.models import IPOD_USB_PIDS
from podsync.hardware.catalog.sysinfo import parse_sysinfo_extended
from podsync.hardware.probes.backend import get_libusb_backend
from podsync.hardware.probes.scsi import APPLE_VID, inquiry_cdb, read_vpd_payload, standard_inquiry_strings

__all__ = ["APPLE_VID", "identify_by_vpd", "main", "query_all_ipods", "query_ipod_vpd", "write_sysinfo"]

logger = logging.getLogger(__name__)

_CLI = "python -m podsync.hardware.probes.libusb"
_BULK_TIMEOUT_MS = 5000
_CSW_LENGTH = 13
_REMOUNT_WAIT_S = 12
_PAUSE_BETWEEN_DEVICES_S = 3


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return callable(geteuid) and geteuid() == 0


# ── bulk-only mass-storage transport ────────────────────────────────


def _usb():
    try:
        import usb.core  # type: ignore[import-not-found]
        import usb.util  # type: ignore[import-not-found]
    except ImportError:
        return None
    return usb


def _device_serial(usb, device) -> str:
    try:
        return str(usb.util.get_string(device, device.iSerialNumber) or "").strip()
    except Exception:
        return ""


def _bulk_inquiry(ep_out, ep_in, tag: int, evpd: bool, page: int, length: int) -> bytes | None:
    """One INQUIRY wrapped in a command block wrapper; ``None`` when the status byte is not 0."""
    cdb = inquiry_cdb(evpd, page, length & 0xFF)
    cbw = struct.pack("<4sIIBBB", b"USBC", tag, length, 0x80, 0, len(cdb)) + cdb.ljust(16, b"\x00")
    ep_out.write(cbw, timeout=_BULK_TIMEOUT_MS)
    data = bytes(ep_in.read(length, timeout=_BULK_TIMEOUT_MS))
    status = bytes(ep_in.read(_CSW_LENGTH, timeout=_BULK_TIMEOUT_MS))
    return data if len(status) >= _CSW_LENGTH and status[12] == 0 else None


def _iter_ipods(usb):
    return (device for device in usb.core.find(find_all=True, idVendor=APPLE_VID, backend=get_libusb_backend()) or []
            if device.idProduct in IPOD_USB_PIDS)


def _detach_kernel_driver(device) -> bool:
    try:
        if device.is_kernel_driver_active(0):
            device.detach_kernel_driver(0)
            return True
    except Exception as exc:
        if "Access denied" in str(exc) or "Operation not permitted" in str(exc):
            raise PermissionError(
                f"Root/sudo required to detach kernel driver for USB VPD query. Run with: sudo {_CLI}"
            ) from exc
        raise
    return False


def _endpoint(usb, interface, direction):
    return usb.util.find_descriptor(
        interface, custom_match=lambda ep: usb.util.endpoint_direction(ep.bEndpointAddress) == direction,
    )


def query_ipod_vpd(usb_pid: int = 0, serial_filter: str = "") -> dict[str, Any] | None:
    """INQUIRY data of the first matching iPod over raw bulk transfers (needs PyUSB, root outside Windows)."""
    usb = _usb()
    if usb is None:
        return None
    wanted = str(serial_filter or "").strip().casefold()
    try:
        device = next((d for d in _iter_ipods(usb) if (not usb_pid or d.idProduct == usb_pid)
                       and (not wanted or _device_serial(usb, d).casefold() == wanted)), None)
    except Exception:
        return None
    if device is None:
        return None
    detached = False
    try:
        detached = sys.platform != "win32" and _detach_kernel_driver(device)
        usb.util.claim_interface(device, 0)
        interface = device.get_active_configuration()[(0, 0)]
        ep_out, ep_in = _endpoint(usb, interface, usb.util.ENDPOINT_OUT), _endpoint(usb, interface, usb.util.ENDPOINT_IN)
        tags = iter(range(2, 1 << 31))

        def inquiry(evpd: bool, page: int, length: int) -> bytes | None:
            return _bulk_inquiry(ep_out, ep_in, next(tags), evpd, page, length)

        payload = read_vpd_payload(inquiry)
        result: dict[str, Any] = {
            "usb_vid": APPLE_VID, "usb_pid": int(device.idProduct), "usb_serial": _device_serial(usb, device),
            "_source": "scsi_vpd", "_transport": "usb_bulk_scsi_vpd",
        }
        result.update(standard_inquiry_strings(inquiry))
        if payload:
            parsed = parse_sysinfo_extended(payload, source="scsi_vpd", live=True)
            result["vpd_raw_xml"] = parsed.raw_xml
            result.update(parsed.plist)
        return result
    finally:
        try:
            usb.util.release_interface(device, 0)
        except Exception:
            pass
        if detached:
            try:
                device.attach_kernel_driver(0)
            except Exception:
                logger.warning("Could not re-attach the iPod kernel driver; reconnect the iPod")


def query_all_ipods() -> list[dict[str, Any]]:
    """Bulk VPD data of every connected iPod (pausing between devices so each can remount)."""
    usb = _usb()
    if usb is None:
        return []
    try:
        devices = list(_iter_ipods(usb))
    except Exception:
        return []
    results: list[dict[str, Any]] = []
    for index, device in enumerate(devices):
        if index:
            time.sleep(_PAUSE_BETWEEN_DEVICES_S)
        try:
            found = query_ipod_vpd(int(device.idProduct), _device_serial(usb, device))
        except PermissionError:
            raise
        except Exception as exc:
            logger.debug("VPD query failed for USB PID 0x%04X: %s", device.idProduct, exc)
            continue
        if found:
            results.append(found)
    return results


# ── SysInfo writing (CLI only) ──────────────────────────────────────


def _xml_payload(raw: Any) -> bytes:
    """The plist part of *raw*, closed when truncated; ``b""`` without one."""
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not isinstance(data, (bytes, bytearray)) or not data:
        return b""
    data = bytes(data)
    start = data.find(b"<?xml")
    start = start if start >= 0 else data.find(b"<plist")
    if start < 0:
        return b""
    data = data[start:]
    return data if b"</plist>" in data else data + b"</dict></plist>"


def _sysinfo_lines(vpd_info: dict[str, Any]) -> list[str]:
    lines = []
    if vpd_info.get("SerialNumber"):
        lines.append(f"pszSerialNumber: {vpd_info['SerialNumber']}")
    if guid := vpd_info.get("FireWireGUID") or vpd_info.get("usb_serial"):
        lines.append(f"FirewireGuid: 0x{guid}")
    if build := vpd_info.get("VisibleBuildID") or vpd_info.get("BuildID"):
        lines.append(f"visibleBuildID: {build}")
    lines.extend(f"{key}: {vpd_info[key]}" for key in ("BoardHwName", "ModelNumStr", "FamilyID", "UpdaterFamilyID")
                 if vpd_info.get(key) not in (None, ""))
    return lines


def write_sysinfo(ipod_path: str, vpd_info: dict[str, Any], *, reported_volume_format: str = "",
                  expected_volume_identity_key: str = "") -> bool:
    """Write SysInfo and SysInfoExtended from live VPD data through the guarded metadata session."""
    lines, xml = _sysinfo_lines(vpd_info), _xml_payload(vpd_info.get("vpd_raw_xml"))
    if not lines and not xml:
        return False
    from podsync.hardware.safety.sysinfo_write import sysinfo_write_session

    with sysinfo_write_session(ipod_path, reported_volume_format=reported_volume_format,
                               expected_volume_identity_key=expected_volume_identity_key) as session:
        if lines:
            session.write_text_atomic("iPod_Control/Device/SysInfo", "\n".join(lines) + "\n",
                                      allowed_subtree="iPod_Control/Device")
        if xml:
            session.write_bytes_atomic("iPod_Control/Device/SysInfoExtended", xml,
                                       allowed_subtree="iPod_Control/Device")
    return True


def _apple_product_serial(vpd_info: dict[str, Any]) -> str:
    return str(vpd_info.get("SerialNumber") or vpd_info.get("vpd_serial") or "").strip()


# ── live payload collection ─────────────────────────────────────────


def _try_source(module_name: str, function: str, **kwargs) -> dict[str, Any] | None:
    """Call a transport imported at call time (tests install stand-ins in ``sys.modules``)."""
    try:
        return getattr(importlib.import_module(module_name), function)(**kwargs)
    except Exception as exc:
        logger.debug("VPD source %s.%s unavailable: %s", module_name, function, exc)
        return None


def _scsi_payload(usb_pid: int, firewire_guid: str, mount_path: str) -> dict[str, Any] | None:
    """The first SCSI transport's reply that carries an Apple serial."""
    filters = {"usb_pid": usb_pid, "serial_filter": firewire_guid}
    if sys.platform == "darwin":
        found = _try_source("podsync.hardware.probes.macos", "query_ipod_vpd", **filters)
        if found:
            found = dict(found)
            if not found.get("SerialNumber") and (serial := _apple_product_serial(found)):
                found["SerialNumber"] = serial
            found.setdefault("_source", "scsi_vpd")
            found.setdefault("_transport", "iokit_scsi_vpd")
            if found.get("SerialNumber"):
                return found
    elif mount_path and (sys.platform == "win32" or sys.platform.startswith("linux")):
        module = "windows" if sys.platform == "win32" else "linux"
        found = _try_source(f"podsync.hardware.probes.{module}", "query_ipod_vpd_for_path",
                            mount_path=mount_path, **filters)
        if found and found.get("SerialNumber"):
            return found
    if sys.platform != "win32" and _is_root():
        try:
            found = query_ipod_vpd(usb_pid=usb_pid, serial_filter=firewire_guid)
        except Exception as exc:
            logger.debug("Bulk VPD query failed: %s", exc)
            found = None
        if found and _apple_product_serial(found):
            found["_used_pyusb"] = True
            return found
    return None


def _merge_vendor(scsi: dict[str, Any], vendor: dict[str, Any]) -> dict[str, Any]:
    """SCSI payload wins conflicts; the vendor payload fills gaps and is kept alongside."""
    merged = dict(scsi)
    filled = {}
    for key, value in vendor.items():
        if not key.startswith("_") and key not in merged:
            merged[key] = value
            filled[key] = "usb_vendor"
    merged.update(
        _raw_field_sources=filled, _usb_vendor_info=vendor, _usb_vendor_raw_xml=vendor.get("vpd_raw_xml", b""),
        _transport=f"{scsi.get('_transport', '')}+{vendor.get('_transport', '')}",
        _source=scsi.get("_source", "vpd"), _used_usb_vendor=True,
    )
    return merged


def _vpd_query_any_platform(usb_pid: int, firewire_guid: str, mount_path: str = "", *,
                            include_usb_vendor: bool | None = None) -> dict[str, Any] | None:
    if include_usb_vendor is None:
        include_usb_vendor = sys.platform != "win32"  # WinUSB is not bound to an iPod by default
    scsi = _scsi_payload(usb_pid, firewire_guid, mount_path)
    vendor = None
    if include_usb_vendor:
        serial_filter = firewire_guid or str((scsi or {}).get("FireWireGUID") or (scsi or {}).get("usb_serial") or "")
        vendor = _try_source("podsync.hardware.probes.usb_control", "query_ipod_usb_sysinfo_extended",
                             usb_pid=usb_pid, serial_filter=serial_filter)
    if scsi and vendor:
        return _merge_vendor(scsi, vendor)
    return scsi or vendor or None


def _unescape_mount(text: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def _find_mount_point_for_usb_serial(usb_serial: str) -> str | None:
    """Where the iPod with this USB serial is mounted again after a bulk query (Linux, macOS)."""
    wanted = str(usb_serial or "").replace(" ", "").upper()
    if not wanted:
        return None
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/mounts", encoding="utf-8", errors="replace") as handle:
                mounts = [line.split() for line in handle if line.startswith("/dev/")]
        except OSError:
            return None
        for parts in (p for p in mounts if len(p) >= 2):
            node = os.path.realpath(f"/sys/block/{re.sub(r'[0-9]+$', '', os.path.basename(parts[0]))}")
            for _ in range(8):
                node = os.path.dirname(node)
                try:
                    with open(os.path.join(node, "serial"), encoding="utf-8") as handle:
                        if handle.read().strip().replace(" ", "").upper() == wanted:
                            return _unescape_mount(parts[1])
                except OSError:
                    continue
        return None
    if sys.platform == "darwin":
        try:
            text = subprocess.run(["mount"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        for line in text.splitlines():
            match = re.match(r"^(\S+) on (.+) \(", line)
            if match and "/Volumes/" in match.group(2):
                return match.group(2)
    return None


def _wait_for_remount(usb_serial: str, mount_path: str) -> str:
    for _ in range(_REMOUNT_WAIT_S):
        if found := _find_mount_point_for_usb_serial(usb_serial):
            return found
        if os.path.ismount(mount_path):
            break
        time.sleep(1)
    return mount_path


def identify_by_vpd(mount_path: str = "", usb_pid: int = 0, firewire_guid: str = "", *,
                    write_sysinfo_to_device: bool = True) -> dict[str, Any] | None:
    """Model identity from the device's own serial number, read live.

    On Windows only the SCSI pass-through transport exists, so a
    *mount_path* (drive letter) is required there.
    """
    if sys.platform == "win32" and not mount_path:
        return None
    payload = _vpd_query_any_platform(usb_pid, firewire_guid, mount_path)
    if not payload:
        return None
    serial = _apple_product_serial(payload)
    if not serial:
        return None
    payload.setdefault("SerialNumber", serial)
    model_number, row = lookup_by_serial(serial) or ("", ("", "", "", ""))
    guid = str(payload.get("FireWireGUID") or payload.get("usb_serial") or "").upper()
    firmware = next((str(payload[key]) for key in ("FireWireVersion", "scsi_revision", "VisibleBuildID", "BuildID")
                     if payload.get(key)), "")
    result: dict[str, Any] = {
        "serial": serial, "firewire_guid": guid or firewire_guid, "firmware": firmware,
        "model_number": model_number, "model_family": row[0], "generation": row[1], "capacity": row[2],
        "color": row[3], "mount_path": mount_path, "sysinfo_written": False, "vpd_info": payload,
        "source": payload.get("_source", "vpd"),
    }
    if payload.get("_transport") == "usb_bulk_scsi_vpd" and mount_path:
        result["mount_path"] = _wait_for_remount(payload.get("usb_serial", ""), mount_path)
    if write_sysinfo_to_device and result["mount_path"] and os.path.isdir(result["mount_path"]):
        try:
            result["sysinfo_written"] = write_sysinfo(result["mount_path"], payload)
        except Exception as exc:
            logger.debug("SysInfo write after VPD identification failed: %s", exc)
    return result


# ── CLI ─────────────────────────────────────────────────────────────


def _print_record(info: dict[str, Any]) -> None:
    pid = int(info.get("usb_pid") or 0)
    print("=" * 60)
    print(f"iPod (USB PID 0x{pid:04X})")
    print("=" * 60)
    rows = [
        ("Apple Serial:", info.get("SerialNumber") or info.get("usb_serial") or "?"),
        ("FireWire GUID:", info.get("FireWireGUID") or info.get("usb_serial") or ""),
        ("FamilyID:", info.get("FamilyID", "")),
        ("UpdaterFamilyID:", info.get("UpdaterFamilyID", "")),
        ("BuildID:", info.get("VisibleBuildID") or info.get("BuildID") or "?"),
        ("SCSI Vendor:", info.get("scsi_vendor", "")),
        ("SCSI Product:", info.get("scsi_product", "")),
        ("SCSI Revision:", info.get("scsi_revision", "")),
    ]
    hit = lookup_by_serial(str(info.get("SerialNumber") or info.get("usb_serial") or ""))
    if hit is not None:
        model, (family, generation, capacity, color) = hit
        rows += [("Model:", f"{family} {generation}".strip()), ("Capacity:", capacity), ("Color:", color),
                 ("Model Number:", model)]
    for label, value in rows:
        print(f"  {label:<17}{value}")


def _cli_mount_for(info: dict[str, Any], given: str) -> str:
    if given:
        return given
    for attempt in range(3):
        if found := _find_mount_point_for_usb_serial(info.get("usb_serial", "")):
            return found
        if attempt < 2:
            time.sleep(5)
    return ""


def main() -> int:
    """Command-line entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(prog=_CLI)
    parser.add_argument("--write-sysinfo", action="store_true")
    parser.add_argument("--pid", default="")
    parser.add_argument("--path", default="")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger().setLevel(logging.DEBUG)
    if sys.platform != "win32" and not _is_root():
        print(f"ERROR: Root privileges required. Run with: sudo {_CLI}")
        return 1
    print("Scanning for iPod USB devices...")
    try:
        results = query_all_ipods()
    except PermissionError as exc:
        print(f"ERROR: {exc}")
        return 1
    if not results:
        print("No iPods found or query failed.")
        return 1
    wanted_pid = int(args.pid, 16) if args.pid else 0
    for info in results:
        pid = int(info.get("usb_pid") or 0)
        if wanted_pid and pid != wanted_pid:
            continue
        _print_record(info)
        if not args.write_sysinfo:
            continue
        if sys.platform != "win32":
            time.sleep(8)  # the kernel re-attaches the disk after the bulk query
        mount = _cli_mount_for(info, args.path)
        if not mount:
            print("  WARNING: could not find the iPod mount point; pass --path")
            continue
        print(f"Writing SysInfo for PID 0x{pid:04X} to {mount}...")
        print("  Done!" if write_sysinfo(mount, info) else "  WARNING: nothing was written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
