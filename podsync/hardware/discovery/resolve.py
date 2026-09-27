"""Combining hardware and filesystem evidence into one model identity.

Hardware evidence (IOCTL, ioreg, sysfs …) is live; filesystem evidence
(SysInfo, SysInfoExtended, the database header) may be stale — an iPod's disk
can be moved to another unit.  Live values therefore win, and every
filesystem value that loses a real disagreement is kept in ``_conflicts``.

Model priority: the serial-number suffix, then the SysInfo model number, then
the USB product id, then the database signature scheme.  A serial or SysInfo
model that contradicts the live USB PID is discarded.
"""

from __future__ import annotations

import logging
from typing import Any

from podsync.hardware.catalog.lookup import get_model_info, lookup_by_serial, usb_pid_identity_conflicts

__all__ = ["EXTRA_FIELDS", "resolve_model"]

logger = logging.getLogger(__name__)

# Copied as-is (hardware first) onto the device record.
EXTRA_FIELDS = (
    "family_id", "updater_family_id", "product_type", "usb_vid", "usb_serial",
    "usbstor_instance_id", "usb_parent_instance_id", "usb_grandparent_instance_id",
    "scsi_vendor", "scsi_product", "scsi_revision", "connected_bus",
    "reported_volume_format", "filesystem_type", "volume_identity_key", "db_version",
    "shadow_db_version", "uses_sqlite_db", "supports_sparse_artwork", "max_tracks",
    "max_file_size_gb", "max_transfer_speed", "podcasts_supported",
    "voice_memos_supported", "audio_codecs", "power_information", "apple_drm_version",
    "artwork_formats", "photo_formats", "chapter_image_formats",
)
_MODEL_PARTS = ("model_family", "generation", "capacity", "color")


def is_empty(value: Any) -> bool:
    """Whether *value* carries no evidence."""
    return value is None or value == "" or value == b"" or value == {} or value == []


def _is_real_serial(serial: str) -> bool:
    return bool(serial) and not serial.upper().startswith("RAND")  # "RAND…" = placeholder from some firmware


class _Resolution:
    def __init__(self, hw: dict, fs: dict) -> None:
        self.hw, self.fs = hw, fs
        self.hw_sources = hw.get("_sources", {}) or {}
        self.fs_sources = fs.get("_sources", {}) or {}
        self.result: dict[str, Any] = {}
        self.sources: dict[str, str] = {}
        self.conflicts: list[dict[str, Any]] = []

    def set(self, name: str, value: Any, source: str | None) -> None:
        self.result[name] = value
        if source is not None:
            self.sources[name] = source

    def prefer_hardware(self, name: str, fs_default: str) -> None:
        if self.hw.get(name):
            self.set(name, self.hw[name], self.hw_sources.get(name, "hardware"))
        elif self.fs.get(name):
            self.set(name, self.fs[name], self.fs_sources.get(name, fs_default))
        else:
            self.set(name, "", None)

    def reject(self, field: str, winner: str, source: str, value: Any, reason: str) -> None:
        self.conflicts.append({"field": field, "winner": winner, "rejected_source": source,
                               "rejected_value": value, "reason": reason})

    def apply_model(self, model: str, row, part_source: str, *, overwrite_sources: bool) -> None:
        self.result["model_number"] = model
        for name, value in zip(_MODEL_PARTS, row):
            self.result[name] = value
            if overwrite_sources:
                self.sources[name] = part_source
            else:
                self.sources.setdefault(name, part_source)


def resolve_model(hw: dict, fs: dict, disk_size_gb: float) -> dict[str, Any]:
    """Resolved identity fields plus ``_sources`` and ``_conflicts``.

    *disk_size_gb* is accepted for callers that pass it; capacity estimation
    from the disk size happens later, in ``enrich``.
    """
    r = _Resolution(hw or {}, fs or {})
    hw, fs = r.hw, r.fs

    for name in EXTRA_FIELDS:
        if not is_empty(hw.get(name)):
            r.set(name, hw[name], r.hw_sources.get(name, "hardware"))
        elif not is_empty(fs.get(name)):
            r.set(name, fs[name], r.fs_sources.get(name, "sysinfo_extended"))

    r.prefer_hardware("firewire_guid", "sysinfo")
    hw_serial, fs_serial = str(hw.get("serial") or "").strip(), str(fs.get("serial") or "").strip()
    if _is_real_serial(hw_serial):
        r.set("serial", hw_serial, r.hw_sources.get("serial", "hardware"))
        if _is_real_serial(fs_serial) and fs_serial.casefold() != hw_serial.casefold():
            r.reject("serial", r.sources["serial"], r.fs_sources.get("serial", "sysinfo"), fs_serial,
                     "cached product serial conflicts with live hardware serial")
    elif _is_real_serial(fs_serial):
        r.set("serial", fs_serial, r.fs_sources.get("serial", "sysinfo"))
    else:
        r.set("serial", "", None)
    r.prefer_hardware("firmware", "sysinfo")

    r.result["usb_pid"] = int(hw.get("usb_pid") or 0)
    if r.result["usb_pid"]:
        r.sources["usb_pid"] = r.hw_sources.get("usb_pid", "hardware")
    scheme = fs.get("hashing_scheme")
    r.result["hashing_scheme"] = int(scheme) if scheme is not None else -1

    pid_family, pid_generation = str(hw.get("model_family") or ""), str(hw.get("generation") or "")
    sysinfo_model = str(fs.get("model_number") or "")
    sysinfo_row = get_model_info(sysinfo_model) if sysinfo_model else None
    serial_hit = lookup_by_serial(r.result["serial"]) if r.result["serial"] else None
    sysinfo_source = r.fs_sources.get("model_number", "sysinfo")

    if pid_family:
        if sysinfo_row is not None and usb_pid_identity_conflicts(sysinfo_row[0], sysinfo_row[1], pid_family,
                                                                  pid_generation):
            logger.warning("SysInfo model %s conflicts with the live USB PID identity", sysinfo_model)
            r.reject("model_number", "usb_pid", sysinfo_source, sysinfo_model, "model conflicts with USB PID identity")
            sysinfo_row = None
        if serial_hit is not None and usb_pid_identity_conflicts(serial_hit[1][0], serial_hit[1][1], pid_family,
                                                                 pid_generation):
            logger.warning("Serial model %s conflicts with the live USB PID identity", serial_hit[0])
            r.reject("model_number", "usb_pid", "serial_lookup", serial_hit[0],
                     "serial model conflicts with USB PID identity")
            serial_hit = None
    if sysinfo_row is not None and serial_hit is not None and serial_hit[0] != sysinfo_model:
        r.reject("model_number", "serial_lookup", sysinfo_source, sysinfo_model, "serial suffix names a different model")
        sysinfo_row = None

    if serial_hit is not None:
        r.sources["model_number"] = "serial_lookup"
        r.apply_model(serial_hit[0], serial_hit[1], r.sources.get("serial", "serial_lookup"), overwrite_sources=True)
        r.result["identification_method"] = "serial"
    elif sysinfo_row is not None:
        r.sources["model_number"] = sysinfo_source
        r.apply_model(sysinfo_model, sysinfo_row, sysinfo_source, overwrite_sources=False)
        r.result["identification_method"] = "sysinfo"
    else:
        r.result.update(model_number=sysinfo_model, model_family="iPod", generation="", capacity="", color="",
                        identification_method="filesystem")
        if pid_family:
            r.result.update(model_family=pid_family, generation=pid_generation, identification_method="usb_pid")
            r.sources["model_family"] = "usb_pid"
            if pid_generation:
                r.sources["generation"] = "usb_pid"
        if r.result["model_family"] == "iPod" and fs.get("hash_model_family"):
            r.result.update(model_family=fs["hash_model_family"], generation=fs.get("hash_generation", ""),
                            identification_method="hashing")
            r.sources.update(model_family="hashing", generation="hashing")

    r.result["_sources"] = r.sources
    r.result["_conflicts"] = r.conflicts
    return r.result
