"""macOS VPD path through IOKit's SCSI Task Device Interface.

Only importable on macOS; everywhere else the import itself fails.

Rendezvous follows Apple's ``SCSITaskLib`` (IOKit/scsi/SCSITaskLib.h) and
tries two ways to find a usable service, in order:

1. ``com_apple_driver_iPodSBCNub`` — Apple's own iPod-specific companion
   driver. Where it matches, it sits beside (not on top of) the mounted
   volume's mass-storage driver, so exclusive access doesn't fight anything.
2. The mounted BSD whole disk's ancestor service that advertises the
   ``SCSITaskDeviceUserClient`` plugin — the same nub the in-kernel
   mass-storage driver itself uses, kept as a fallback for systems where the
   iPod nub never matched.

Either way, once a service is found: get a ``SCSITaskDeviceInterface`` for it
through ``IOCreatePlugInInterfaceForService``/``QueryInterface``, then create
one ``SCSITask`` and run every INQUIRY the shared VPD reader
(:mod:`podsync.hardware.probes.scsi`) asks for on it (each preceded by
``ResetForNewTask``, since a task must be reset before it can be reused).

The user client only lets one Logical Unit Driver control a device at a
time, so path 2's ``CreateSCSITask`` legitimately returns ``NULL`` while
something else — usually the in-kernel mass-storage driver, while the volume
is mounted — already holds that seat; this probe reports nothing for that
disk rather than fight the OS for it. It never attempts to unmount or force
anything.

Known gap, confirmed on macOS 27.0 (build 26A428): ``ObtainExclusiveAccess``
reports success (and stays reported as success on a repeat call) yet
``IsExclusiveAccessAvailable`` never flips and ``CreateSCSITask`` still
returns ``NULL`` afterwards — on *both* paths, including path 1's dedicated
nub, which normally shouldn't contend with anything. That rules out FSKit
(the volume's userspace filesystem extension, e.g.
``com.apple.fskit.msdos.appex``) as the sole explanation; exclusive access
looks like a no-op on this build rather than a real hand-off. Older macOS
with the classic in-kernel mass-storage driver is expected to be unaffected;
this module still calls ``ObtainExclusiveAccess``/``ReleaseExclusiveAccess``
around every task because that is the documented, correct sequence and it is
free when the device is already free.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import subprocess
from typing import Any

from podsync.hardware.discovery.macos import parse_ioreg_bsd_serials
from podsync.hardware.probes.scsi import build_vpd_result, inquiry_cdb

__all__ = ["query_all_ipods", "query_ipod_vpd"]

logger = logging.getLogger(__name__)

_INQUIRY_CDB_SIZE = 6
_kSCSIDataTransfer_FromTargetToInitiator = 2
_TIMEOUT_MS = 5000
_IPOD_SBC_NUB_CLASS = b"com_apple_driver_iPodSBCNub"


def _load_iokit():
    try:
        iokit_path = ctypes.util.find_library("IOKit")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not iokit_path or not cf_path:
            return None, None
        return ctypes.cdll.LoadLibrary(iokit_path), ctypes.cdll.LoadLibrary(cf_path)
    except OSError:
        return None, None


_IOKit, _CF = _load_iokit()

c_void_p = ctypes.c_void_p
_io_object_t = ctypes.c_uint32
_IOReturn = ctypes.c_int32
_Boolean = ctypes.c_ubyte


class _CFUUIDBytes(ctypes.Structure):
    _fields_ = [(f"b{i}", ctypes.c_uint8) for i in range(16)]


class _SCSI_Sense_Data(ctypes.Structure):
    _fields_ = [("raw", ctypes.c_uint8 * 252)]


class _IOAddressRange(ctypes.Structure):
    _fields_ = [("address", ctypes.c_uint64), ("length", ctypes.c_uint64)]


_QueryInterfaceFn = ctypes.CFUNCTYPE(_IOReturn, c_void_p, _CFUUIDBytes, ctypes.POINTER(c_void_p))
_AddRefFn = ctypes.CFUNCTYPE(ctypes.c_uint32, c_void_p)
_ReleaseFn = ctypes.CFUNCTYPE(ctypes.c_uint32, c_void_p)


class _IOCFPlugInInterfaceStruct(ctypes.Structure):
    _fields_ = [
        ("_reserved", c_void_p),
        ("QueryInterface", _QueryInterfaceFn),
        ("AddRef", _AddRefFn),
        ("Release", _ReleaseFn),
        ("version", ctypes.c_uint16),
        ("revision", ctypes.c_uint16),
    ]


# An "interface" handle in CFPlugIn's C-COM convention is T** (self, passed to every
# method, IS this T** value — see IOCreatePlugInInterfaceForService and SCSITaskLib.h).
_IOCFPlugInInterfaceHandle = ctypes.POINTER(ctypes.POINTER(_IOCFPlugInInterfaceStruct))


class _SCSITaskInterfaceStruct(ctypes.Structure):
    _fields_ = [
        ("_reserved", c_void_p),
        ("QueryInterface", _QueryInterfaceFn),
        ("AddRef", _AddRefFn),
        ("Release", _ReleaseFn),
        ("version", ctypes.c_uint16),
        ("revision", ctypes.c_uint16),
        ("IsTaskActive", ctypes.CFUNCTYPE(_Boolean, c_void_p)),
        ("SetTaskAttribute", ctypes.CFUNCTYPE(_IOReturn, c_void_p, ctypes.c_uint32)),
        ("GetTaskAttribute", ctypes.CFUNCTYPE(_IOReturn, c_void_p, ctypes.POINTER(ctypes.c_uint32))),
        ("SetCommandDescriptorBlock",
         ctypes.CFUNCTYPE(_IOReturn, c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint8)),
        ("GetCommandDescriptorBlockSize", ctypes.CFUNCTYPE(ctypes.c_uint8, c_void_p)),
        ("GetCommandDescriptorBlock", ctypes.CFUNCTYPE(_IOReturn, c_void_p, ctypes.POINTER(ctypes.c_uint8))),
        ("SetScatterGatherEntries", ctypes.CFUNCTYPE(
            _IOReturn, c_void_p, ctypes.POINTER(_IOAddressRange), ctypes.c_uint8, ctypes.c_uint64, ctypes.c_uint8)),
        ("SetTimeoutDuration", ctypes.CFUNCTYPE(_IOReturn, c_void_p, ctypes.c_uint32)),
        ("GetTimeoutDuration", ctypes.CFUNCTYPE(ctypes.c_uint32, c_void_p)),
        ("SetTaskCompletionCallback", ctypes.CFUNCTYPE(_IOReturn, c_void_p, c_void_p, c_void_p)),
        ("ExecuteTaskAsync", ctypes.CFUNCTYPE(_IOReturn, c_void_p)),
        ("ExecuteTaskSync", ctypes.CFUNCTYPE(
            _IOReturn, c_void_p, ctypes.POINTER(_SCSI_Sense_Data), ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint64))),
        ("AbortTask", ctypes.CFUNCTYPE(_IOReturn, c_void_p)),
        ("GetSCSIServiceResponse", ctypes.CFUNCTYPE(_IOReturn, c_void_p, ctypes.POINTER(ctypes.c_uint32))),
        ("GetTaskState", ctypes.CFUNCTYPE(_IOReturn, c_void_p, ctypes.POINTER(ctypes.c_uint32))),
        ("GetTaskStatus", ctypes.CFUNCTYPE(_IOReturn, c_void_p, ctypes.POINTER(ctypes.c_uint32))),
        ("GetRealizedDataTransferCount", ctypes.CFUNCTYPE(ctypes.c_uint64, c_void_p)),
        ("GetAutoSenseData", ctypes.CFUNCTYPE(_IOReturn, c_void_p, ctypes.POINTER(_SCSI_Sense_Data))),
        ("SetAutoSenseDataBuffer",
         ctypes.CFUNCTYPE(_IOReturn, c_void_p, ctypes.POINTER(_SCSI_Sense_Data), ctypes.c_uint8)),
        ("ResetForNewTask", ctypes.CFUNCTYPE(_IOReturn, c_void_p)),
    ]


_SCSITaskInterfaceHandle = ctypes.POINTER(ctypes.POINTER(_SCSITaskInterfaceStruct))


class _SCSITaskDeviceInterfaceStruct(ctypes.Structure):
    _fields_ = [
        ("_reserved", c_void_p),
        ("QueryInterface", _QueryInterfaceFn),
        ("AddRef", _AddRefFn),
        ("Release", _ReleaseFn),
        ("version", ctypes.c_uint16),
        ("revision", ctypes.c_uint16),
        ("IsExclusiveAccessAvailable", ctypes.CFUNCTYPE(_Boolean, c_void_p)),
        ("AddCallbackDispatcherToRunLoop", ctypes.CFUNCTYPE(_IOReturn, c_void_p, c_void_p)),
        ("RemoveCallbackDispatcherFromRunLoop", ctypes.CFUNCTYPE(None, c_void_p)),
        ("ObtainExclusiveAccess", ctypes.CFUNCTYPE(_IOReturn, c_void_p)),
        ("ReleaseExclusiveAccess", ctypes.CFUNCTYPE(_IOReturn, c_void_p)),
        ("CreateSCSITask", ctypes.CFUNCTYPE(_SCSITaskInterfaceHandle, c_void_p)),
    ]


_SCSITaskDeviceInterfaceHandle = ctypes.POINTER(ctypes.POINTER(_SCSITaskDeviceInterfaceStruct))


def _configure_ctypes() -> None:
    """Signatures for every C entry point this module calls; a no-op if IOKit didn't load."""
    if _IOKit is None or _CF is None:
        return
    _CF.CFUUIDGetConstantUUIDWithBytes.restype = c_void_p
    _CF.CFUUIDGetConstantUUIDWithBytes.argtypes = [c_void_p] + [ctypes.c_uint8] * 16
    _CF.CFUUIDGetUUIDBytes.restype = _CFUUIDBytes
    _CF.CFUUIDGetUUIDBytes.argtypes = [c_void_p]
    _CF.CFStringCreateWithCString.restype = c_void_p
    _CF.CFStringCreateWithCString.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    _CF.CFRelease.argtypes = [c_void_p]
    _CF.CFDictionaryGetValue.restype = c_void_p
    _CF.CFDictionaryGetValue.argtypes = [c_void_p, c_void_p]
    _CF.CFGetTypeID.restype = ctypes.c_ulong
    _CF.CFGetTypeID.argtypes = [c_void_p]
    _CF.CFDictionaryGetTypeID.restype = ctypes.c_ulong
    _CF.CFNumberGetTypeID.restype = ctypes.c_ulong
    _CF.CFNumberGetValue.restype = ctypes.c_bool
    _CF.CFNumberGetValue.argtypes = [c_void_p, ctypes.c_int32, c_void_p]
    _CF.CFStringGetTypeID.restype = ctypes.c_ulong
    _CF.CFStringGetCString.restype = ctypes.c_bool
    _CF.CFStringGetCString.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]

    _IOKit.IOServiceGetMatchingService.restype = _io_object_t
    _IOKit.IOServiceGetMatchingService.argtypes = [c_void_p, c_void_p]
    _IOKit.IOServiceMatching.restype = c_void_p
    _IOKit.IOServiceMatching.argtypes = [ctypes.c_char_p]
    _IOKit.IOServiceGetMatchingServices.restype = _IOReturn
    _IOKit.IOServiceGetMatchingServices.argtypes = [c_void_p, c_void_p, ctypes.POINTER(_io_object_t)]
    _IOKit.IOIteratorNext.restype = _io_object_t
    _IOKit.IOIteratorNext.argtypes = [_io_object_t]
    _IOKit.IOBSDNameMatching.restype = c_void_p
    _IOKit.IOBSDNameMatching.argtypes = [c_void_p, ctypes.c_uint32, ctypes.c_char_p]
    _IOKit.IORegistryEntryGetParentEntry.restype = _IOReturn
    _IOKit.IORegistryEntryGetParentEntry.argtypes = [_io_object_t, ctypes.c_char_p, ctypes.POINTER(_io_object_t)]
    _IOKit.IORegistryEntryCreateCFProperty.restype = c_void_p
    _IOKit.IORegistryEntryCreateCFProperty.argtypes = [_io_object_t, c_void_p, c_void_p, ctypes.c_uint32]
    _IOKit.IOObjectRelease.argtypes = [_io_object_t]
    _IOKit.IOCreatePlugInInterfaceForService.restype = _IOReturn
    _IOKit.IOCreatePlugInInterfaceForService.argtypes = [
        _io_object_t, c_void_p, c_void_p, ctypes.POINTER(_IOCFPlugInInterfaceHandle), ctypes.POINTER(ctypes.c_int32),
    ]
    _IOKit.IODestroyPlugInInterface.argtypes = [_IOCFPlugInInterfaceHandle]


_configure_ctypes()

_UUID_SCSI_TASK_DEVICE_USER_CLIENT = (
    0x7D, 0x66, 0x67, 0x8E, 0x08, 0xA2, 0x11, 0xD5, 0xA1, 0xB8, 0x00, 0x30, 0x65, 0x7D, 0x05, 0x2A)
_UUID_IOCFPLUGIN_INTERFACE = (
    0xC2, 0x44, 0xE8, 0x58, 0x10, 0x9C, 0x11, 0xD4, 0x91, 0xD4, 0x00, 0x50, 0xE4, 0xC6, 0x42, 0x6F)
_UUID_SCSI_TASK_DEVICE_INTERFACE = (
    0x1B, 0xBC, 0x41, 0x32, 0x08, 0xA5, 0x11, 0xD5, 0x90, 0xED, 0x00, 0x30, 0x65, 0x7D, 0x05, 0x2A)
_SCSI_TASK_DEVICE_USER_CLIENT_TYPE_ID_STR = "7D66678E-08A2-11D5-A1B8-0030657D052A"
_kCFNumberSInt32Type = 3


def _cfstr(cf, text: str):
    return cf.CFStringCreateWithCString(None, text.encode(), 0x08000100)  # kCFStringEncodingUTF8


def _registry_property_int(io_kit, cf, entry: int, key: str) -> int | None:
    cf_key = _cfstr(cf, key)
    try:
        value = io_kit.IORegistryEntryCreateCFProperty(entry, cf_key, None, 0)
        if not value:
            return None
        try:
            if cf.CFGetTypeID(value) != cf.CFNumberGetTypeID():
                return None
            out = ctypes.c_int32(0)
            cf.CFNumberGetValue(value, _kCFNumberSInt32Type, ctypes.byref(out))
            return out.value
        finally:
            cf.CFRelease(value)
    finally:
        cf.CFRelease(cf_key)


def _registry_property_str(io_kit, cf, entry: int, key: str) -> str:
    cf_key = _cfstr(cf, key)
    try:
        value = io_kit.IORegistryEntryCreateCFProperty(entry, cf_key, None, 0)
        if not value:
            return ""
        try:
            if cf.CFGetTypeID(value) != cf.CFStringGetTypeID():
                return ""
            buf = ctypes.create_string_buffer(256)
            if not cf.CFStringGetCString(value, buf, 256, 0x08000100):
                return ""
            return buf.value.decode("utf-8", errors="replace")
        finally:
            cf.CFRelease(value)
    finally:
        cf.CFRelease(cf_key)


def _usb_identity_from_ancestors(io_kit, cf, service: int) -> dict[str, Any]:
    """``idProduct``/``idVendor``/``USB Serial Number`` off the nearest ancestor that has them.

    *service* itself is left untouched (the caller still owns it); every
    intermediate parent this walks is released before returning.
    """
    result: dict[str, Any] = {}
    entry = service
    owns_entry = False
    for _ in range(10):
        parent = _io_object_t(0)
        kr = io_kit.IORegistryEntryGetParentEntry(entry, b"IOService", ctypes.byref(parent))
        if owns_entry:
            io_kit.IOObjectRelease(entry)
        if kr != 0 or not parent.value:
            break
        entry, owns_entry = parent.value, True
        if (pid := _registry_property_int(io_kit, cf, entry, "idProduct")) is not None:
            result.setdefault("usb_pid", pid)
        if (vid := _registry_property_int(io_kit, cf, entry, "idVendor")) is not None:
            result.setdefault("usb_vid", vid)
        if serial := _registry_property_str(io_kit, cf, entry, "USB Serial Number"):
            result.setdefault("usb_serial", serial)
        if "usb_pid" in result and "usb_serial" in result:
            break
    if owns_entry:
        io_kit.IOObjectRelease(entry)
    return result


def _has_plugin_type(io_kit, cf, service: int, type_uuid_str: str) -> bool:
    key = _cfstr(cf, "IOCFPlugInTypes")
    try:
        prop = io_kit.IORegistryEntryCreateCFProperty(service, key, None, 0)
        if not prop:
            return False
        try:
            if cf.CFGetTypeID(prop) != cf.CFDictionaryGetTypeID():
                return False
            uuid_key = _cfstr(cf, type_uuid_str)
            try:
                return bool(cf.CFDictionaryGetValue(prop, uuid_key))
            finally:
                cf.CFRelease(uuid_key)
        finally:
            cf.CFRelease(prop)
    finally:
        cf.CFRelease(key)


def _find_scsi_task_service_for_bsd_disk(io_kit, cf, bsd_name: str) -> int | None:
    """The nearest ancestor of *bsd_name* that vends the SCSITaskDeviceUserClient plugin."""
    matching = io_kit.IOBSDNameMatching(0, 0, bsd_name.encode())
    if not matching:
        return None
    entry = io_kit.IOServiceGetMatchingService(0, matching)
    if not entry:
        return None
    seen = [entry]
    current = entry
    for _ in range(10):
        if _has_plugin_type(io_kit, cf, current, _SCSI_TASK_DEVICE_USER_CLIENT_TYPE_ID_STR):
            for stale in seen[:-1]:
                io_kit.IOObjectRelease(stale)
            return current
        parent = _io_object_t(0)
        kr = io_kit.IORegistryEntryGetParentEntry(current, b"IOService", ctypes.byref(parent))
        if kr != 0 or not parent.value:
            break
        current = parent.value
        seen.append(current)
    for stale in seen:
        io_kit.IOObjectRelease(stale)
    return None


def _ipod_sbc_nub_services(io_kit) -> list[int]:
    """Every live instance of Apple's iPod-specific ``SBCNub`` companion driver."""
    matching = io_kit.IOServiceMatching(_IPOD_SBC_NUB_CLASS)
    if not matching:
        return []
    iterator = _io_object_t(0)
    if io_kit.IOServiceGetMatchingServices(0, matching, ctypes.byref(iterator)) != 0:
        return []
    services = []
    while service := io_kit.IOIteratorNext(iterator.value):
        services.append(service)
    io_kit.IOObjectRelease(iterator.value)
    return services


def _open_scsi_task_device_for_service(io_kit, cf, service: int):
    """A live ``(vtable, self_ptr, handle)`` for *service*'s SCSITaskDeviceInterface, or ``None``.

    Consumes (releases) *service* either way.
    """
    plugin = _IOCFPlugInInterfaceHandle()
    score = ctypes.c_int32(0)
    plugin_type = cf.CFUUIDGetConstantUUIDWithBytes(None, *_UUID_SCSI_TASK_DEVICE_USER_CLIENT)
    plugin_interface_id = cf.CFUUIDGetConstantUUIDWithBytes(None, *_UUID_IOCFPLUGIN_INTERFACE)
    kr = io_kit.IOCreatePlugInInterfaceForService(
        service, plugin_type, plugin_interface_id, ctypes.byref(plugin), ctypes.byref(score))
    io_kit.IOObjectRelease(service)
    if kr != 0 or not plugin:
        return None

    device_interface_uuid = cf.CFUUIDGetConstantUUIDWithBytes(None, *_UUID_SCSI_TASK_DEVICE_INTERFACE)
    device_iid = cf.CFUUIDGetUUIDBytes(device_interface_uuid)
    plugin_self = ctypes.cast(plugin, c_void_p)
    dev_iface = _SCSITaskDeviceInterfaceHandle()
    hres = plugin.contents.contents.QueryInterface(
        plugin_self, device_iid, ctypes.cast(ctypes.byref(dev_iface), ctypes.POINTER(c_void_p)))
    io_kit.IODestroyPlugInInterface(plugin)
    if hres != 0 or not dev_iface:
        return None
    return dev_iface.contents.contents, ctypes.cast(dev_iface, c_void_p), dev_iface


def _scsi_task_inquiry(task_vtable, task_self, evpd: bool, page: int, length: int) -> bytes | None:
    task_vtable.ResetForNewTask(task_self)
    cdb_bytes = inquiry_cdb(evpd, page, length)
    cdb = (ctypes.c_uint8 * _INQUIRY_CDB_SIZE)(*cdb_bytes)
    if task_vtable.SetCommandDescriptorBlock(task_self, cdb, _INQUIRY_CDB_SIZE) != 0:
        return None
    buf = (ctypes.c_uint8 * length)()
    sg = _IOAddressRange(address=ctypes.cast(buf, c_void_p).value, length=length)
    if task_vtable.SetScatterGatherEntries(
            task_self, ctypes.byref(sg), 1, length, _kSCSIDataTransfer_FromTargetToInitiator) != 0:
        return None
    task_vtable.SetTimeoutDuration(task_self, _TIMEOUT_MS)
    sense = _SCSI_Sense_Data()
    status = ctypes.c_uint32(0)
    transferred = ctypes.c_uint64(0)
    kr = task_vtable.ExecuteTaskSync(task_self, ctypes.byref(sense), ctypes.byref(status), ctypes.byref(transferred))
    if kr != 0:
        return None
    return bytes(buf[:transferred.value])


def _query_service(service: int, *, usb_pid: int = 0, serial_filter: str = "") -> dict[str, Any] | None:
    """VPD for the SCSITaskDeviceUserClient-capable *service* (consumed either way)."""
    opened = _open_scsi_task_device_for_service(_IOKit, _CF, service)
    if opened is None:
        return None
    dev_vtable, dev_self, dev_handle = opened
    try:
        dev_vtable.ObtainExclusiveAccess(dev_self)
        task_iface = dev_vtable.CreateSCSITask(dev_self)
        if not task_iface:
            # Another Logical Unit Driver (almost always the in-kernel one, while
            # mounted) already holds the device; nothing more to try here.
            logger.debug("CreateSCSITask returned NULL for service %s (device busy)", service)
            return None
        try:
            task_vtable = task_iface.contents.contents
            task_self = ctypes.cast(task_iface, c_void_p)

            def inquiry(evpd: bool, page: int, length: int) -> bytes | None:
                return _scsi_task_inquiry(task_vtable, task_self, evpd, page, length)

            return build_vpd_result(
                inquiry, source="ioreg_scsi", transport="iokit_scsi_task", usb_pid=usb_pid, serial_filter=serial_filter)
        finally:
            task_vtable.Release(task_self)
    finally:
        dev_vtable.ReleaseExclusiveAccess(dev_self)
        dev_vtable.Release(dev_self)


def _query_via_sbc_nub() -> list[dict[str, Any]]:
    results = []
    for service in _ipod_sbc_nub_services(_IOKit):
        usb_identity = _usb_identity_from_ancestors(_IOKit, _CF, service)
        if found := _query_service(service, usb_pid=usb_identity.get("usb_pid", 0)):
            results.append({**usb_identity, **found})
    return results


def _query_via_mounted_disks() -> list[dict[str, Any]]:
    try:
        completed = subprocess.run(
            ["ioreg", "-r", "-c", "IOMedia"], capture_output=True, text=True, timeout=10, check=False)
        bsd_names = sorted(parse_ioreg_bsd_serials(completed.stdout))
    except Exception as exc:
        logger.debug("Listing iPod BSD disks failed: %s", exc)
        return []
    results = []
    for bsd_name in bsd_names:
        service = _find_scsi_task_service_for_bsd_disk(_IOKit, _CF, bsd_name)
        if service is None:
            continue
        if found := _query_service(service):
            results.append(found)
    return results


def query_all_ipods() -> list[dict[str, Any]]:
    """VPD payloads for every reachable iPod: Apple's SBCNub first, mounted disks as fallback."""
    if _IOKit is None or _CF is None:
        logger.debug("IOKit is unavailable")
        return []
    return _query_via_sbc_nub() or _query_via_mounted_disks()


def query_ipod_vpd(usb_pid: int = 0, serial_filter: str = "") -> dict[str, Any] | None:
    for result in query_all_ipods():
        if usb_pid and int(result.get("usb_pid") or 0) != usb_pid:
            continue
        if serial_filter and str(result.get("usb_serial", "")).upper() != serial_filter.upper():
            continue
        return result
    return None
