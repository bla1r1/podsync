"""Hardware evidence for a mounted iPod on Windows.

The direct storage IOCTL (:func:`podsync.hardware.probes.windows.storage_identity`)
is tried first: it takes milliseconds and also yields the USB PID from the
PnP tree.  Only when it finds nothing does the scan fall back to asking the
management service through PowerShell (slow, about a second) and to the
registry for the PID.
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import Any

from podsync.hardware.catalog.models import USB_PID_TO_MODEL
from podsync.hardware.probes import windows as windows_probe

__all__ = ["probe_windows_hardware"]

logger = logging.getLogger(__name__)

_HEX16 = re.compile(r"[0-9A-Fa-f]{16}")
_WMI_TIMEOUT_S = 30


def _wmi_disk_line(letter: str) -> str:
    """``PNPDeviceID<TAB>SerialNumber<TAB>Model`` of the disk behind *letter*, or ``""``."""
    script = (
        "$ErrorActionPreference='Stop';"
        f"$ld=Get-CimInstance Win32_LogicalDisk -Filter \"DeviceID='{letter}:'\";"
        "$p=Get-CimAssociatedInstance -InputObject $ld -ResultClassName Win32_DiskPartition;"
        "$d=Get-CimAssociatedInstance -InputObject $p -ResultClassName Win32_DiskDrive;"
        "Write-Output ($d.PNPDeviceID + \"`t\" + $d.SerialNumber + \"`t\" + $d.Model)"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=_WMI_TIMEOUT_S, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("WMI disk query for %s: failed: %s", letter, exc)
        return ""
    lines = (completed.stdout or "").strip().splitlines()
    return lines[-1] if completed.returncode == 0 and lines else ""


def _from_wmi(letter: str) -> dict[str, Any]:
    parts = _wmi_disk_line(letter).split("\t")
    pnp = parts[0]
    serial = parts[1].strip().replace(" ", "") if len(parts) > 1 else ""
    result: dict[str, Any] = {}
    if _HEX16.fullmatch(serial):
        result["firewire_guid"] = serial.upper()
    elif serial:
        result["serial"] = serial
    if pnp.upper().startswith("USBSTOR"):
        result["usbstor_instance_id"] = pnp
        if revision := re.search(r"REV_([^\\&]+)", pnp):
            result["firmware"] = revision.group(1)
        if guid := windows_probe.guid_from_instance_id(pnp):
            result.setdefault("firewire_guid", guid)
    return result


def _add_registry_pid(result: dict[str, Any]) -> None:
    vid, pid = windows_probe.usb_ids_from_registry(result.get("firewire_guid", ""))
    if not pid:
        return
    result.update(usb_vid=vid, usb_pid=pid)
    family, generation = USB_PID_TO_MODEL.get(pid, ("", ""))
    if family:
        result.setdefault("model_family", family)
    if generation:
        result.setdefault("generation", generation)


def probe_windows_hardware(mount_name: str) -> tuple[dict[str, Any], str]:
    """``(evidence, method)`` for the drive named by *mount_name*'s first letter."""
    letter = (mount_name or "")[:1].upper()
    if not letter.isalpha():
        return {}, "ioctl"
    found = windows_probe.storage_identity(f"{letter}:\\")
    if found:
        return found, "ioctl"
    found = _from_wmi(letter)
    if found.get("firewire_guid"):
        _add_registry_pid(found)
    return found, "wmi"
