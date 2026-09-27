"""Read-only JSON report of everything podsync can learn about a mounted iPod.

``python -m podsync.hardware.diagnostics.dump [PATH …] [--all] [--include-raw] [--no-usb-vendor]``

The report lists each identity fact with the source that supplied it (cached
SysInfo files, the OS hardware probe, live SCSI VPD, Apple's USB vendor
request), the identity the scanner would settle on, and every piece of
evidence that disagrees with it.  Raw bytes are summarised as length and
SHA-256 unless ``--include-raw`` is given.  Nothing is written to the device.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from podsync.hardware.catalog.sysinfo import (
    identity_from_sysinfo,
    identity_from_sysinfo_extended,
    normalize_guid,
    parse_sysinfo_extended,
    parse_sysinfo_text,
)
from podsync.hardware.discovery.resolve import resolve_model

__all__ = ["IDENTITY_FIELDS", "dump_device_info", "main"]

IDENTITY_FIELDS: tuple[str, ...] = (
    "serial", "firewire_guid", "model_number", "model_family", "generation",
    "capacity", "color", "firmware", "board", "family_id", "updater_family_id",
    "product_type", "usb_vid", "usb_pid", "usb_serial", "scsi_vendor",
    "scsi_product", "scsi_revision", "connected_bus", "reported_volume_format",
    "filesystem_type", "db_version", "shadow_db_version", "uses_sqlite_db",
    "supports_sparse_artwork", "max_tracks", "max_file_size_gb",
    "max_transfer_speed", "podcasts_supported", "voice_memos_supported",
)
_LIVE_PASSTHROUGH = ("usb_pid", "usb_vid", "usb_serial", "scsi_vendor", "scsi_product", "scsi_revision", "block_device")
_USB_DETAILS = ("usb_vid", "usb_pid", "firewire_guid", "usbstor_instance_id", "usb_parent_instance_id",
                "usb_grandparent_instance_id")


def _device_dir(mount: str) -> Path:
    return Path(mount) / "iPod_Control" / "Device"


def _mount_name(mount: str) -> str:
    if sys.platform == "win32":
        drive = os.path.splitdrive(mount)[0]
        if drive:
            return drive
        if mount[:1].isalpha():
            return f"{mount[0].upper()}:"
    return os.path.basename(os.path.normpath(mount)) or mount


def _normalise_mount_path(mount: str) -> str:
    if sys.platform != "win32":
        return mount
    text = str(mount).strip().strip('"')
    return text + "\\" if len(text) == 2 and text[1] == ":" else text


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_safe(value: Any, *, include_raw: bool = False) -> Any:
    """Bytes become ``{bytes, sha256[, text]}``; dict keys are stringified and sorted."""
    if isinstance(value, (bytes, bytearray)):
        summary: dict[str, Any] = {"bytes": len(value), "sha256": _sha256(bytes(value))}
        if include_raw:
            summary["text"] = bytes(value).decode("utf-8", errors="replace")
        return summary
    if isinstance(value, dict):
        return {str(k): _json_safe(value[k], include_raw=include_raw) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, include_raw=include_raw) for item in value]
    return value


def _comparable(field: str, value: Any) -> str:
    if value is None or value == "":
        return ""
    if field == "firewire_guid":
        return normalize_guid(value)
    if field == "usb_pid":
        try:
            return f"0x{int(value):04X}"
        except (TypeError, ValueError):
            return str(value).strip().upper()
    return str(value).strip()


def _add_evidence(evidence: dict, data: dict, default_source: str) -> None:
    sources = data.get("_sources", {}) or {}
    for field in IDENTITY_FIELDS:
        value = data.get(field)
        if value is not None and value != "":
            evidence.setdefault(field, []).append({"source": sources.get(field, default_source), "value": value})


def _rejected_conflicts(final: dict, evidence: dict) -> list[dict[str, Any]]:
    rejected = []
    for field in sorted(evidence):
        final_value = _comparable(field, final.get(field))
        if not final_value:
            continue
        for entry in evidence[field]:
            value = _comparable(field, entry.get("value"))
            if value and value != final_value:
                rejected.append({"field": field, "final_value": final.get(field), "rejected_value": entry.get("value"),
                                 "rejected_source": entry.get("source")})
    return rejected


def _disk_size_gb(mount: str) -> float:
    try:
        return round(shutil.disk_usage(mount).total / 1e9, 1)
    except OSError:
        return 0.0


def _final_identity_snapshot(mount: str) -> dict[str, Any]:
    from podsync.hardware.discovery import scan

    hardware = scan._probe_hardware(mount, _mount_name(mount))
    filesystem = scan._probe_filesystem(mount)
    return {"hardware": hardware, "filesystem": filesystem,
            "resolved": resolve_model(hardware, filesystem, _disk_size_gb(mount))}


def _identity_from_live_vpd(result: dict, source: str) -> dict[str, Any]:
    raw = result.get("vpd_raw_xml")
    if raw:
        identity = parse_sysinfo_extended(raw, source=source, live=True).identity
    else:
        identity = identity_from_sysinfo_extended(dict(result), source, live=True)
    sources = identity.setdefault("_sources", {})
    for field in _LIVE_PASSTHROUGH:
        value = result.get(field)
        if value not in (None, "", b""):
            identity[field] = value
            sources[field] = source
    return identity


def _live_scsi_query(mount: str, usb_pid: int, guid: str) -> tuple[dict | None, str] | None:
    """``(result, default source)`` from this platform's SCSI transport; ``None`` when there is none."""
    if sys.platform == "win32":
        from podsync.hardware.probes import windows

        return windows.query_ipod_vpd_for_path(mount, usb_pid=usb_pid, serial_filter=guid), "windows_scsi"
    if sys.platform.startswith("linux"):
        from podsync.hardware.probes import linux

        return linux.query_ipod_vpd_for_path(mount, usb_pid=usb_pid, serial_filter=guid), "linux_scsi"
    if sys.platform == "darwin":
        from podsync.hardware.probes import macos

        return macos.query_ipod_vpd(usb_pid=usb_pid, serial_filter=guid), "scsi_vpd"
    return None


def _live_scsi(mount: str, usb_pid: int, guid: str) -> dict[str, Any]:
    try:
        answer = _live_scsi_query(mount, usb_pid, guid)
    except Exception as exc:
        return {"available": True, "result": None, "error": repr(exc)}
    if answer is None:
        return {"available": False, "reason": f"unsupported_{sys.platform}"}
    result, default = answer
    if not result:
        return {"available": True, "result": None, "error": "no_result"}
    return {
        "available": True,
        "result": result,
        "identity": _identity_from_live_vpd(result, result.get("_source") or default),
        "standard_inquiry": {"vendor": result.get("scsi_vendor", ""), "product": result.get("scsi_product", ""),
                             "revision": result.get("scsi_revision", "")},
    }


def _live_usb_vendor(usb_pid: int, guid: str, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {"available": False, "reason": "disabled"}
    from podsync.hardware.probes.backend import backend_diagnostic

    try:
        from podsync.hardware.probes import usb_control

        result = usb_control.query_ipod_usb_sysinfo_extended(usb_pid=usb_pid, serial_filter=guid)
    except Exception as exc:
        return {"available": False, "result": None, "error": repr(exc)}
    if not result:
        return {"available": True, "result": None, "error": "no_result", "backend": backend_diagnostic()}
    return {"available": True, "result": result,
            "identity": _identity_from_live_vpd(result, result.get("_source") or "usb_vendor"),
            "backend": backend_diagnostic()}


def _sysinfo_section(mount: str) -> dict[str, Any]:
    path = _device_dir(mount) / "SysInfo"
    if not path.is_file():
        return {"present": False}
    text = path.read_bytes().decode("utf-8", errors="replace")
    fields = parse_sysinfo_text(text)
    return {"present": True, "path": str(path), "sha256": _sha256(text.encode("utf-8", errors="replace")),
            "fields": fields, "identity": identity_from_sysinfo(fields, "sysinfo")}


def _extended_section(mount: str, include_raw: bool) -> dict[str, Any]:
    path = _device_dir(mount) / "SysInfoExtended"
    if not path.is_file():
        return {"present": False}
    raw = path.read_bytes()
    parsed = parse_sysinfo_extended(raw)
    section: dict[str, Any] = {
        "present": True, "path": str(path), "bytes": len(raw), "sha256": _sha256(raw),
        "used_regex_fallback": parsed.used_regex_fallback, "repaired": parsed.repaired, "keys": sorted(parsed.plist),
        "identity": parsed.identity, "cover_art_formats": parsed.cover_art_formats,
        "photo_formats": parsed.photo_formats, "chapter_image_formats": parsed.chapter_image_formats,
    }
    if include_raw:
        section.update(raw=raw, plist=parsed.plist)
    return section


def dump_device_info(mount_path: str, *, include_raw: bool = False, probe_usb_vendor: bool = True) -> dict[str, Any]:
    """The JSON-safe diagnostic report for one mount."""
    mount = os.path.abspath(_normalise_mount_path(mount_path))
    snapshot = _final_identity_snapshot(mount)
    hardware = snapshot["hardware"] or {}
    sysinfo = _sysinfo_section(mount)
    extended = _extended_section(mount, include_raw)

    usb_pid = int(hardware.get("usb_pid") or 0)
    guid = str(hardware.get("firewire_guid") or "")
    live_scsi = _live_scsi(mount, usb_pid, guid)
    live_windows = live_scsi if sys.platform == "win32" else {"available": False, "reason": "not_windows"}
    live_vendor = _live_usb_vendor(usb_pid, guid, probe_usb_vendor)

    evidence: dict[str, list] = {}
    if sysinfo.get("present"):
        _add_evidence(evidence, sysinfo["identity"], "sysinfo")
    if extended.get("present"):
        _add_evidence(evidence, extended["identity"], "sysinfo_extended")
    _add_evidence(evidence, hardware, "hardware")
    if live_scsi.get("identity"):
        _add_evidence(evidence, live_scsi["identity"], "live_scsi")
    if live_vendor.get("identity"):
        _add_evidence(evidence, live_vendor["identity"], "usb_vendor")

    final = snapshot["resolved"]
    report = {
        "mount_path": mount,
        "mount_name": _mount_name(mount),
        "sysinfo": sysinfo,
        "disk_sysinfo_extended": extended,
        "live_scsi_vpd": live_scsi,
        "live_windows_scsi_vpd": live_windows,
        "live_usb_vendor": live_vendor,
        "standard_inquiry": live_scsi.get("standard_inquiry", {}),
        "usb_details": {key: hardware[key] for key in _USB_DETAILS if hardware.get(key) not in (None, "")},
        "final_resolved_identity": final,
        "resolver_conflicts": final.get("_conflicts", []),
        "all_identity_evidence": evidence,
        "rejected_conflicting_evidence": _rejected_conflicts(final, evidence),
    }
    return _json_safe(report, include_raw=include_raw)


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point; prints the JSON report and returns the exit code."""
    parser = argparse.ArgumentParser(prog="python -m podsync.hardware.diagnostics.dump")
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--include-raw", action="store_true")
    parser.add_argument("--no-usb-vendor", action="store_true")
    args = parser.parse_args(argv)
    paths = list(args.paths)
    if args.all or not paths:
        from podsync.hardware.discovery import scan

        paths += [mount for mount, _display in scan._find_ipod_volumes() if mount not in paths]
    if not paths:
        print("No iPod volumes found.", file=sys.stderr)
        return 1
    reports = [dump_device_info(path, include_raw=args.include_raw, probe_usb_vendor=not args.no_usb_vendor)
               for path in paths]
    print(json.dumps(reports[0] if len(reports) == 1 else reports, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
