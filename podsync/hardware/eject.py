"""Ejecting an iPod safely on Windows, macOS and Linux.

Rules that hold on every platform:

* the volume at the path must still be the iPod that was selected (same
  identity key, own mount point, ``iPod_Control`` present), and nothing is
  ejected while another podsync writer holds the device lock;
* pending writes are flushed first (unless mounted read-only) and a failed
  flush aborts the eject — a half-written database is worse than a busy iPod;
* nothing is ever forced: busy volumes are reported, not detached;
* success is only claimed once the mount is really gone.

Each platform tries its gentlest mechanism first and falls back step by step:
Windows lock/dismount → Configuration Manager eject request → Explorer's
"Eject" verb; macOS ``diskutil eject`` → ``unmount`` + ``eject``; Linux
``udisksctl`` → ``eject`` → plain ``umount`` (plus power-off when possible).
"""

from __future__ import annotations

import json
import logging
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from podsync.hardware.safety.durable import flush_volume
from podsync.hardware.safety.fsprofile import VolumeProfile, profile_volume
from podsync.hardware.safety.guard import UnsafeWriteError, WriteLock
from podsync.hardware.safety.readiness import lock_key_for

__all__ = ["eject_device"]

logger = logging.getLogger(__name__)

_TIMEOUT_SECS = 30
_WINDOWS_VERIFY_SECS = 20
_WINDOWS_LOCK_RETRY_SECS = 10
_UNMOUNT_VERIFY_SECS = 10
_POLL_SECS = 0.25

_FSCTL_LOCK_VOLUME = 0x00090018
_FSCTL_DISMOUNT_VOLUME = 0x00090020
_IOCTL_STORAGE_MEDIA_REMOVAL = 0x002D4804
_IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808
_DRIVE_NO_ROOT_DIR = 1

# Output that means "nothing left to do" rather than a failure.
_ALREADY_UNMOUNTED_HINTS = ("not mounted", "not currently mounted", "already unmounted")
_MISSING_TARGET_HINTS = ("no such file or directory", "not found", "no object", "error looking up object",
                         "does not exist")


def _benign(message: str) -> bool:
    text = str(message or "").casefold()
    return any(hint in text for hint in _ALREADY_UNMOUNTED_HINTS + _MISSING_TARGET_HINTS)


def _unique(items) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _flush_failure(message: str) -> tuple[bool, str]:
    return False, f"podsync could not flush pending writes, so the iPod was not ejected. {message}"


def _wait_until(condition: Callable[[], bool], seconds: float) -> bool:
    """Poll *condition* until it holds or *seconds* pass; one last check after the deadline."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(_POLL_SECS)
    return condition()


def _same_path(a, b) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


# ── orchestration ───────────────────────────────────────────────────


def _inspect_eject_volume(path: Path, *, reported_volume_format: str = "") -> VolumeProfile:
    """Profile the volume and refuse anything that is not the iPod's own, verifiable mount."""
    profile = profile_volume(path, reported_volume_format=reported_volume_format, probe_case_sensitivity=False)
    if not _same_path(path, profile.mount_path):
        raise UnsafeWriteError("The selected iPod path is no longer mounted as its own volume. podsync stopped "
                               "rather than ejecting the containing host volume.")
    if not (path / "iPod_Control").is_dir():
        raise UnsafeWriteError("The selected volume no longer contains iPod_Control. podsync stopped rather than "
                               "ejecting an unrecognized volume.")
    if not profile.identity.is_complete:
        raise UnsafeWriteError("The mounted volume identity could not be verified. Use the operating system's "
                               "eject control for this iPod.")
    logger.debug("Eject volume inspected: mount=%s filesystem=%s reported=%s source=%s key=%s", profile.mount_path,
                 profile.filesystem_type, profile.reported_volume_format, profile.mount_source, lock_key_for(profile))
    return profile


def _revalidate_eject_volume(retained: VolumeProfile) -> VolumeProfile:
    """The profile again, under the lock; raises if identity, filesystem or mount point moved."""
    current = profile_volume(retained.inspection_path or retained.mount_path,
                             reported_volume_format=retained.reported_volume_format, probe_case_sensitivity=False)
    if not current.identity.is_complete or current.identity != retained.identity:
        raise UnsafeWriteError("The mounted volume changed while podsync was preparing to eject. Nothing was "
                               "ejected; reconnect and reload the iPod.")
    if current.filesystem_type != retained.filesystem_type:
        raise UnsafeWriteError("The filesystem at the selected iPod path changed while preparing to eject. "
                               "Nothing was ejected.")
    if not _same_path(current.mount_path, retained.mount_path):
        raise UnsafeWriteError("The iPod mount point changed while preparing to eject. Nothing was ejected.")
    return current


def eject_device(mount_path: str, *, reported_volume_format: str = "",
                 expected_volume_identity_key: str = "") -> tuple[bool, str]:
    """Flush and eject the iPod at *mount_path*: ``(ejected, message for the user)``."""
    if not mount_path:
        return False, "No device path supplied."
    path = Path(os.path.realpath(mount_path))
    if (path / "iPodInfo.json").is_file():
        return True, "Virtual iPod closed; no operating-system eject was needed."
    try:
        profile = _inspect_eject_volume(path, reported_volume_format=reported_volume_format)
        key = lock_key_for(profile)
        if expected_volume_identity_key and expected_volume_identity_key != key:
            raise UnsafeWriteError("A different volume is mounted at the selected iPod path. podsync stopped "
                                   "before ejecting it. Reconnect and reload the iPod.")
        with WriteLock(path, volume_key=key, track_database_generation=False):
            read_only = bool(_revalidate_eject_volume(profile).read_only)
            logger.info("Ejecting %s (read_only=%s, platform=%s)", path, read_only, sys.platform)
            if sys.platform == "win32":
                return _eject_windows(path, read_only=read_only)
            if sys.platform == "darwin":
                return _eject_macos(path, read_only=read_only)
            return _eject_linux(path, read_only=read_only)
    except UnsafeWriteError as exc:
        logger.warning("Safe eject refused for %s: %s", mount_path, exc)
        return False, str(exc)
    except Exception as exc:
        logger.exception("eject_device: unexpected failure")
        return False, f"Unexpected error: {exc}"


# ── shared command runner ───────────────────────────────────────────


def _run_command(args: list[str], timeout: int = _TIMEOUT_SECS) -> tuple[bool, str]:
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return False, f"{args[0]} is not available."
    except subprocess.TimeoutExpired:
        return False, f"{' '.join(args)} timed out."
    output = (completed.stderr or completed.stdout or "").strip()
    if completed.returncode == 0:
        return True, output or f"{' '.join(args)} succeeded."
    return False, output or f"{' '.join(args)} failed with code {completed.returncode}."


# ── Windows ─────────────────────────────────────────────────────────


def _windows_drive_from_path(path: Path) -> str:
    match = re.match(r"^([a-zA-Z]):", str(path)) or re.match(r"^([a-zA-Z]):", path.drive or "")
    return f"{match.group(1).upper()}:" if match else ""


def _windows_drive_is_mounted(drive: str) -> bool:
    if sys.platform != "win32":
        return Path(f"{drive}\\").exists()
    import ctypes

    return ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\") != _DRIVE_NO_ROOT_DIR


def _wait_for_windows_drive_removed(drive: str) -> bool:
    return _wait_until(lambda: not _windows_drive_is_mounted(drive), _WINDOWS_VERIFY_SECS)


def _parse_windows_eject_result(output: str) -> tuple[str, str] | None:
    """``(OK|MISSING|ERROR, message)`` from the first tab-separated status line of a script."""
    for line in str(output or "").splitlines():
        if match := re.match(r"^(OK|MISSING|ERROR)\t(.*)$", line.strip("\r")):
            return match.group(1), match.group(2)
    return None


def _ps_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_powershell(script: str, default_error: str) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=_TIMEOUT_SECS, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        return False, "PowerShell is not available on this system."
    except subprocess.TimeoutExpired:
        return False, "Eject timed out."
    parsed = _parse_windows_eject_result(completed.stdout)
    if parsed is not None:
        status, message = parsed
        if status in ("OK", "MISSING") and completed.returncode == 0:
            return True, message
        return False, message or default_error
    text = (completed.stderr or completed.stdout or "").strip()
    if completed.returncode != 0:
        return False, text or f"{default_error} PowerShell exited with code {completed.returncode}."
    return True, text or default_error


def _windows_pnp_device_id(drive: str) -> tuple[str | None, str]:
    """``(disk instance id or None, explanation)``: PnP tree first, the management service as fallback."""
    from podsync.hardware.probes import windows as windows_probe

    if instance := windows_probe.disk_instance_id(f"{drive}\\"):
        return instance, instance
    script = (
        "$ErrorActionPreference='Stop';try{"
        f"$ld=Get-CimInstance Win32_LogicalDisk -Filter (\"DeviceID='\"+{_ps_literal(drive)}+\"'\");"
        "if(-not $ld){Write-Output \"MISSING`tlogical disk not found\";exit 0};"
        "$p=Get-CimAssociatedInstance -InputObject $ld -ResultClassName Win32_DiskPartition;"
        "$d=Get-CimAssociatedInstance -InputObject $p -ResultClassName Win32_DiskDrive;"
        "Write-Output (\"OK`t\"+$d.PNPDeviceID)}catch{Write-Output (\"ERROR`t\"+$_)}"
    )
    ok, message = _run_powershell(script, "Could not resolve the Windows disk device.")
    found = ok and message and not message.lower().startswith("logical disk")
    return (message if found else None), message


def _prepare_windows_volume_for_eject(drive: str) -> tuple[bool, str]:
    """Flush, lock (retrying while handles close), dismount, allow removal and eject the media."""
    import ctypes
    from ctypes import wintypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError) as exc:
        raise RuntimeError(f"kernel32 is unavailable: {exc}") from exc
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                         ctypes.c_void_p]
    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateFileW(f"\\\\.\\{drive}", 0xC0000000, 0x3, None, 3, 0, None)
    if handle in (None, 0, ctypes.c_void_p(-1).value):
        return False, f"Could not open {drive} for eject ({ctypes.get_last_error()})."
    returned = wintypes.DWORD(0)

    def ioctl(code: int, in_buffer=None, in_size: int = 0) -> bool:
        return bool(kernel32.DeviceIoControl(handle, code, in_buffer, in_size, None, 0, ctypes.byref(returned), None))

    try:
        if not kernel32.FlushFileBuffers(handle):
            return False, f"Could not flush pending writes for {drive} before eject ({ctypes.get_last_error()})."
        last_error = [0]

        def try_lock() -> bool:
            if ioctl(_FSCTL_LOCK_VOLUME):
                return True
            last_error[0] = ctypes.get_last_error()
            return False

        if not _wait_until(try_lock, _WINDOWS_LOCK_RETRY_SECS):
            return False, (f"Could not lock {drive} for eject ({last_error[0]}). Another process still has "
                           "the iPod volume open.")
        if not ioctl(_FSCTL_DISMOUNT_VOLUME):
            return False, f"Locked {drive}, but Windows refused to dismount it ({ctypes.get_last_error()})."
        allow_removal = ctypes.c_ubyte(0)
        ioctl(_IOCTL_STORAGE_MEDIA_REMOVAL, ctypes.byref(allow_removal), 1)  # results ignored: best effort
        ioctl(_IOCTL_STORAGE_EJECT_MEDIA)
        return True, f"Locked and dismounted {drive}."
    finally:
        kernel32.CloseHandle(handle)


def _run_windows_cfgmgr_eject(drive: str, pnp_id: str | None) -> tuple[bool, str]:
    """Configuration Manager eject of the disk's device node (the "Safely Remove" path)."""
    if not pnp_id:
        return False, f"Windows could not identify the device node for {drive}."
    from podsync.hardware.probes import windows as windows_probe

    try:
        error, veto_type, veto_name = windows_probe.request_device_eject(pnp_id)
    except (AttributeError, OSError) as exc:
        return False, f"Configuration Manager is unavailable: {exc}"
    if error == 0 and veto_type == 0:
        return True, f"Windows accepted the eject request for {drive}."
    if veto_type:
        return False, f"Windows vetoed eject for {drive} (CM error {error}, veto {veto_type}, {veto_name})."
    return False, f"Windows could not eject the device node for {drive} (CM error {error})."


def _run_windows_shell_eject(drive: str) -> tuple[bool, str]:
    script = (
        f"$drive={_ps_literal(drive)};$shell=New-Object -ComObject Shell.Application;"
        "$ns=$shell.Namespace(17);if(-not $ns){Write-Output \"ERROR`tExplorer did not expose the Computer namespace.\";exit 0};"
        "$item=$ns.ParseName($drive);if(-not $item){Write-Output \"MISSING`t$drive is not mounted.\";exit 0};"
        "$item.InvokeVerb('Eject');Write-Output \"OK`tExplorer accepted the eject request for $drive.\""
    )
    return _run_powershell(script, "Explorer eject request failed.")


def _eject_windows(path: Path, *, read_only: bool = False) -> tuple[bool, str]:
    drive = _windows_drive_from_path(path)
    if not drive:
        return False, f"Cannot determine drive letter from {path}."
    if not _windows_drive_is_mounted(drive):
        return True, f"{drive} is already ejected."
    if not read_only:
        flushed, flush_message = flush_volume(path)
        if not flushed:
            return _flush_failure(flush_message)
    pnp_id, pnp_message = _windows_pnp_device_id(drive)
    details = [] if pnp_id else [pnp_message]
    for name, step in (("lock/dismount", lambda: _prepare_windows_volume_for_eject(drive)),
                       ("cfgmgr", lambda: _run_windows_cfgmgr_eject(drive, pnp_id)),
                       ("explorer", lambda: _run_windows_shell_eject(drive))):
        ok, message = step()
        logger.debug("Windows eject step %s for %s: ok=%s %s", name, drive, ok, message)
        if ok and _wait_for_windows_drive_removed(drive):
            return True, message or f"Ejected {drive}"
        details.append(message)
    text = (f"Windows did not eject {drive}; the drive is still mounted. Close any File Explorer "
            "windows or apps using the iPod, then try again.")
    if unique := _unique(details):
        text += "\n\nDetails: " + " | ".join(unique)
    return False, text


# ── macOS ───────────────────────────────────────────────────────────


def _macos_disk_info(target: str) -> tuple[dict, str]:
    if not shutil.which("diskutil"):
        return {}, "diskutil is not available."
    try:
        completed = subprocess.run(["diskutil", "info", "-plist", target], capture_output=True, timeout=10,
                                   check=False)
    except FileNotFoundError:
        return {}, "diskutil is not available."
    except subprocess.TimeoutExpired:
        return {}, "diskutil info timed out."
    if completed.returncode != 0:
        output = (completed.stderr or completed.stdout or b"").decode("utf-8", errors="replace").strip()
        return {}, output or "diskutil info failed."
    try:
        info = plistlib.loads(completed.stdout)
    except Exception as exc:
        return {}, f"Could not parse diskutil info: {exc}"
    if not isinstance(info, dict):
        return {}, "diskutil info returned an unexpected response."
    return info, ""


def _macos_mount_is_present(mount_point: str, device_id: str) -> bool:
    """Whether ``mount`` still lists the mount point or the device (unknown counts as present)."""
    try:
        completed = subprocess.run(["mount"], capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return True
    except FileNotFoundError:
        return Path(mount_point).exists()
    if completed.returncode != 0:
        return Path(mount_point).exists()
    wanted = Path(mount_point).resolve(strict=False)
    for line in completed.stdout.splitlines():
        if match := re.match(r"^(.+) on (.+) \(.+\)$", line):
            source, point = match.groups()
            if Path(point).resolve(strict=False) == wanted or (device_id and source == f"/dev/{device_id}"):
                return True
    return False


def _wait_for_macos_mount_gone(mount_point: str, device_id: str) -> bool:
    return _wait_until(lambda: not _macos_mount_is_present(mount_point, device_id), _UNMOUNT_VERIFY_SECS)


def _eject_macos(path: Path, *, read_only: bool = False) -> tuple[bool, str]:
    if not shutil.which("diskutil"):
        return False, "diskutil is not available."
    mount = str(path)
    info, info_message = _macos_disk_info(mount)
    device_id = str(info.get("DeviceIdentifier") or "")
    whole_disk = device_id if info.get("WholeDisk") else str(info.get("ParentWholeDisk") or "")
    disk_target = whole_disk or device_id or mount
    volume_target = device_id or mount
    mount_point = str(info.get("MountPoint") or mount)
    if not info and not _macos_mount_is_present(mount_point, device_id):
        return True, "Device already unmounted."
    if not read_only:
        flushed, flush_message = flush_volume(mount, allow_unavailable=True)
        if not flushed:
            return _flush_failure(flush_message)

    def gone() -> bool:
        return _wait_for_macos_mount_gone(mount_point, device_id)

    details = [info_message]
    ok, message = _run_command(["diskutil", "eject", disk_target])
    details.append(message)
    if ok and gone():
        return True, f"Ejected {disk_target}"
    ok, message = _run_command(["diskutil", "unmount", volume_target])
    details.append(message)
    if ok:
        eject_ok, eject_message = _run_command(["diskutil", "eject", disk_target])
        details.append(eject_message)
        if (eject_ok or _benign(eject_message)) and gone():
            return True, f"Unmounted and ejected {disk_target}"
    if gone():
        return True, f"Device unmounted: {disk_target}"
    text = (f"diskutil could not safely eject {disk_target}; the iPod is still mounted. Close any "
            "files or apps using it, then retry.")
    if unique := _unique(details):
        text += " Details: " + " | ".join(unique)
    return False, text


# ── Linux ───────────────────────────────────────────────────────────


def _decode_octal(text: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def _normalize(path: str) -> str:
    return os.path.normpath(str(path))


def _is_inside(path: str, mount_point: str) -> bool:
    return path == mount_point or path.startswith(mount_point + os.sep)


def _linux_mount_entries() -> list[tuple[str, str]]:
    """``(source, mount point)`` pairs from ``/proc/mounts``; raises ``OSError`` when unreadable."""
    with open("/proc/mounts", encoding="utf-8", errors="replace") as handle:
        return [(parts[0], _decode_octal(parts[1])) for parts in (line.split() for line in handle) if len(parts) >= 2]


def _parent_block_device(device: str | None) -> str | None:
    if not device:
        return None
    directory, name = os.path.split(device)
    for pattern in (r"^(nvme\d+n\d+)p\d+$", r"^(mmcblk\d+)p\d+$", r"^([a-z]+)\d+$"):
        if match := re.match(pattern, name):
            return f"{directory}/{match.group(1)}" if directory else match.group(1)
    return None


def _block_device_from_findmnt(mount: str, wanted: str) -> str | None:
    try:
        completed = subprocess.run(["findmnt", "-n", "-o", "TARGET,SOURCE", "--target", mount],
                                   capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (completed.stdout or "").strip().splitlines()
    parts = lines[0].split() if completed.returncode == 0 and lines else []
    if len(parts) >= 2:
        target, source = _normalize(parts[0]), parts[1]
        if source.startswith("/dev/") and target != "/" and _is_inside(wanted, target):
            return source
    return None


def _block_device_from_mount_table(wanted: str) -> str | None:
    best, best_len = None, -1
    try:
        entries = _linux_mount_entries()
    except OSError:
        return None
    for device, point in entries:
        point = _normalize(point)
        if point != "/" and device.startswith("/dev/") and _is_inside(wanted, point) and len(point) > best_len:
            best, best_len = device, len(point)
    return best


def _block_device_from_lsblk(mount: str) -> str | None:
    try:
        completed = subprocess.run(["lsblk", "-J", "-o", "NAME,MOUNTPOINT"], capture_output=True, text=True,
                                   timeout=5, check=False)
        tree = json.loads(completed.stdout or "{}")
        for disk in tree.get("blockdevices", []) or []:
            for child in disk.get("children", []) or []:
                if child.get("mountpoint") == mount:
                    return f"/dev/{child.get('name')}"
    except Exception:
        pass
    return None


def _find_block_device(mount: str) -> str | None:
    """findmnt first; an exact lsblk partition match beats the longest-prefix mount-table guess."""
    wanted = _normalize(mount)
    return (_block_device_from_findmnt(mount, wanted) or _block_device_from_lsblk(mount)
            or _block_device_from_mount_table(wanted))


def _linux_path_is_mounted(mount: str, device: str | None = None) -> bool:
    try:
        entries = _linux_mount_entries()
    except OSError as exc:
        raise OSError(f"Could not read the Linux mount table: {exc}") from exc
    wanted = _normalize(mount)
    for source, point in entries:
        point = _normalize(point)
        if (device and source == device) or point == wanted:
            return True
        if point != "/" and source.startswith("/dev/") and wanted.startswith(point + os.sep) and device is None:
            return True
    return False


def _wait_for_linux_mount_gone(mount: str, device: str | None = None) -> bool:
    deadline = time.monotonic() + _UNMOUNT_VERIFY_SECS
    while True:
        try:
            if not _linux_path_is_mounted(mount, device):
                return True
        except OSError as exc:
            logger.warning("Could not confirm that %s was unmounted: %s", mount, exc)
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_SECS)


def _run_sync(mount: str | None = None) -> tuple[bool, str]:
    return flush_volume(mount, allow_unavailable=True)


def _run_udisks_unmount(device: str) -> tuple[bool, str]:
    ok, message = _run_command(["udisksctl", "unmount", "--block-device", device, "--no-user-interaction"])
    if ok:
        return True, f"udisksctl unmounted {device}."
    if _benign(message):
        return True, "Device already unmounted."
    return False, message or "udisksctl unmount failed."


def _run_udisks_poweroff(parent: str) -> tuple[bool, str]:
    ok, message = _run_command(["udisksctl", "power-off", "--block-device", parent, "--no-user-interaction"])
    if ok:
        return True, f"Ejected {parent}"
    if _benign(message):
        return True, f"Device already detached: {parent}"
    return False, message or "udisksctl power-off failed."


def _udisks_eject(device: str, mount: str) -> tuple[bool, str]:
    ok, message = _run_udisks_unmount(device)
    if not ok:
        return False, message
    if not _wait_for_linux_mount_gone(mount, device):
        return False, message or f"udisksctl did not unmount {device}."
    parent = _parent_block_device(device)
    return _run_udisks_poweroff(parent) if parent else (True, f"Unmounted {device}")


def _run_umount_command(target: str) -> tuple[bool, str, bool]:
    """``(ok, message, was already unmounted)`` for a plain, non-forced ``umount``."""
    try:
        completed = subprocess.run(["umount", target], capture_output=True, text=True, timeout=_TIMEOUT_SECS,
                                   check=False)
    except FileNotFoundError:
        return False, "umount is not available.", False
    except subprocess.TimeoutExpired:
        return False, "umount timed out.", False
    output = (completed.stderr or completed.stdout or "").strip()
    if completed.returncode == 0:
        return True, f"umount succeeded for {target}", False
    if _benign(output):
        return True, output or "Device already unmounted.", True
    return False, output or "umount failed.", False


def _post_unmount_detach(target: str | None) -> str:
    """Power the disk off after a plain umount when a tool for it exists; message on success."""
    if not target:
        return ""
    if shutil.which("udisksctl"):
        ok, message = _run_udisks_poweroff(target)
    elif shutil.which("eject"):
        ok, message = _run_command(["eject", target])
        ok = ok or _benign(message)
    else:
        return ""
    return message if ok else ""


def _eject_linux(path: Path, *, read_only: bool = False) -> tuple[bool, str]:
    mount = str(path)
    device = _find_block_device(mount)
    detach_target = _parent_block_device(device) or device
    try:
        mounted = _linux_path_is_mounted(mount, device)
    except OSError as exc:
        return False, ("podsync could not verify the Linux mount table, so it did not attempt to eject "
                       f"the iPod: {exc}")
    if not mounted:
        return True, "Device already unmounted."
    if not read_only:
        flushed, flush_message = _run_sync(mount)
        if not flushed:
            return _flush_failure(flush_message)

    def gone() -> bool:
        return _wait_for_linux_mount_gone(mount, device)

    errors: list[str] = []
    if device and shutil.which("udisksctl"):
        ok, message = _udisks_eject(device, mount)
        if ok:
            return True, message
        errors.append(message)
    if detach_target and shutil.which("eject"):
        ok, message = _run_command(["eject", detach_target])
        if ok and gone():
            return True, f"Ejected {detach_target}"
        if _benign(message) and gone():
            return True, f"Device already detached: {detach_target}"
        errors.append(message)
    if shutil.which("umount"):
        for target in _unique([device or "", mount if path.exists() else ""]):
            ok, message, _already = _run_umount_command(target)
            if ok and gone():
                detach = _post_unmount_detach(detach_target)
                return True, f"{message}; {detach}" if detach else message
            errors.append(message)
    if gone():
        return True, "Device already unmounted."
    return False, (" | ".join(_unique(errors))
                   or "No suitable unmount utility found (tried udisksctl, eject, and umount without forcing).")
