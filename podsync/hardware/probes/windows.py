"""Windows device probes built on DeviceIoControl and the PnP tree.

* :func:`storage_identity` — ``IOCTL_STORAGE_QUERY_PROPERTY`` on the volume
  gives vendor, product, revision and serial; ``IOCTL_STORAGE_GET_DEVICE_NUMBER``
  plus a walk over the present disk interfaces finds the disk's USBSTOR
  instance and its USB parent, which carries ``VID_05AC&PID_xxxx``.
* :func:`query_ipod_vpd_for_path` — SCSI INQUIRY through
  ``IOCTL_SCSI_PASS_THROUGH_DIRECT`` for the live SysInfoExtended.
* :func:`usb_ids_from_registry` — the fallback PID lookup under
  ``HKLM\\SYSTEM\\CurrentControlSet\\Enum\\USB``.

None of these need administrator rights for a USB mass-storage iPod.  Every
Win32 call is declared with pointer-sized handles so 64-bit Python does not
truncate them.  The pure parsers at the top are platform-independent.
"""

from __future__ import annotations

import logging
import re
import struct
import sys
from contextlib import contextmanager
from typing import Any

from podsync.hardware.catalog.models import USB_PID_TO_MODEL
from podsync.hardware.probes.scsi import APPLE_VID, InquiryFn, build_vpd_result, inquiry_cdb

__all__ = [
    "guid_from_instance_id", "parse_device_number", "parse_storage_descriptor", "query_ipod_vpd_for_path",
    "storage_identity", "usb_ids_from_instance_id", "usb_ids_from_registry",
]

logger = logging.getLogger(__name__)

_IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
_IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x002D1080
_IOCTL_SCSI_PASS_THROUGH_DIRECT = 0x0004D014
_SCSI_IOCTL_DATA_IN = 1
_GENERIC_READ, _GENERIC_WRITE = 0x80000000, 0x40000000
_FILE_SHARE_READ_WRITE = 0x1 | 0x2
_OPEN_EXISTING = 3
_DIGCF_PRESENT, _DIGCF_DEVICEINTERFACE = 0x2, 0x10
_CR_SUCCESS = 0
_GUID_DEVINTERFACE_DISK = "{53F56307-B6BF-11D0-94F2-00A0C91EFB8B}"
_APPLE_VENDORS = {"apple", "apple inc", "apple inc."}
_USB_ENUM_KEY = r"SYSTEM\CurrentControlSet\Enum\USB"


# ── pure parsers ────────────────────────────────────────────────────


def _drive_letter(mount_path: str) -> str:
    match = re.match(r"^([A-Za-z]):", str(mount_path or ""))
    return match.group(1).upper() if match else ""


def _c_string(buffer: bytes, offset: int) -> str:
    if not offset or offset >= len(buffer):
        return ""
    return buffer[offset:].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()


def parse_storage_descriptor(buffer: bytes) -> dict[str, Any]:
    """Fields of a ``STORAGE_DEVICE_DESCRIPTOR`` (strings are at the offsets it stores)."""
    if len(buffer) < 36:
        return {}
    removable = buffer[10]
    vendor_at, product_at, revision_at, serial_at, bus_type = struct.unpack_from("<5I", buffer, 12)
    return {
        "vendor": _c_string(buffer, vendor_at),
        "product": _c_string(buffer, product_at),
        "revision": _c_string(buffer, revision_at),
        "serial": _c_string(buffer, serial_at),
        "bus_type": bus_type,
        "removable": bool(removable),
    }


def parse_device_number(buffer: bytes) -> tuple[int, int] | None:
    """``(device type, device number)`` from a ``STORAGE_DEVICE_NUMBER``."""
    if len(buffer) < 8:
        return None
    return struct.unpack_from("<2I", buffer)


def guid_from_instance_id(instance_id: str) -> str:
    """The 16-hex FireWire GUID in a USBSTOR/USB instance id's last part, upper case, or ``""``."""
    parts = str(instance_id or "").split("\\")
    if len(parts) < 3:
        return ""
    return next((seg.upper() for seg in parts[2].split("&") if re.fullmatch(r"[0-9A-Fa-f]{16}", seg)), "")


def usb_ids_from_instance_id(instance_id: str) -> tuple[int, int]:
    """``(vid, pid)`` from ``USB\\VID_05AC&PID_1209\\…`` (0 for a missing part)."""
    text = str(instance_id or "").upper()
    vid = re.search(r"VID_([0-9A-F]{4})", text)
    pid = re.search(r"PID_([0-9A-F]{4})", text)
    return (int(vid.group(1), 16) if vid else 0, int(pid.group(1), 16) if pid else 0)


def _descriptor_identity(descriptor: dict[str, Any]) -> dict[str, Any]:
    """Identity fields from an Apple storage descriptor; ``{}`` for any other vendor."""
    if str(descriptor.get("vendor", "")).casefold() not in _APPLE_VENDORS:
        return {}
    result: dict[str, Any] = {}
    if descriptor.get("revision"):
        result["firmware"] = descriptor["revision"]
    serial = str(descriptor.get("serial", "")).replace(" ", "")
    if re.fullmatch(r"[0-9A-Fa-f]{16}", serial):
        result["firewire_guid"] = serial.upper()
    elif serial:
        result["serial"] = serial
    result["scsi_vendor"] = descriptor.get("vendor", "")
    result["scsi_product"] = descriptor.get("product", "")
    result["scsi_revision"] = descriptor.get("revision", "")
    return result


def _tree_identity(disk_id: str, parent_id: str, grandparent_id: str) -> dict[str, Any]:
    """Instance ids, GUID and USB ids from the disk's PnP ancestry."""
    result: dict[str, Any] = {}
    if disk_id:
        result["usbstor_instance_id"] = disk_id
        if guid := guid_from_instance_id(disk_id):
            result["firewire_guid"] = guid
    if parent_id:
        result["usb_parent_instance_id"] = parent_id
        vid, pid = usb_ids_from_instance_id(parent_id)
        if vid:
            result["usb_vid"] = vid
        if pid:
            result["usb_pid"] = pid
            family, generation = USB_PID_TO_MODEL.get(pid, ("", ""))
            if family:
                result["model_family"] = family
            if generation:
                result["generation"] = generation
        if "MI_" in parent_id.upper() and grandparent_id:  # composite device: the GUID sits one level up
            result["usb_grandparent_instance_id"] = grandparent_id
            if "firewire_guid" not in result and (guid := guid_from_instance_id(grandparent_id)):
                result["firewire_guid"] = guid
    return result


# ── Win32 plumbing ──────────────────────────────────────────────────

_api: Any = None


def _win32():
    """kernel32/setupapi/cfgmgr32 with argtypes declared once per process."""
    global _api
    if _api is not None:
        return _api
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID), ("Flags", wintypes.DWORD),
                    ("Reserved", ctypes.c_size_t)]

    class SP_DEVINFO_DATA(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", GUID), ("DevInst", wintypes.DWORD),
                    ("Reserved", ctypes.c_size_t)]

    class SCSI_PASS_THROUGH_DIRECT(ctypes.Structure):
        _fields_ = [
            ("Length", ctypes.c_ushort), ("ScsiStatus", ctypes.c_ubyte), ("PathId", ctypes.c_ubyte),
            ("TargetId", ctypes.c_ubyte), ("Lun", ctypes.c_ubyte), ("CdbLength", ctypes.c_ubyte),
            ("SenseInfoLength", ctypes.c_ubyte), ("DataIn", ctypes.c_ubyte), ("DataTransferLength", ctypes.c_ulong),
            ("TimeOutValue", ctypes.c_ulong), ("DataBuffer", ctypes.c_void_p), ("SenseInfoOffset", ctypes.c_ulong),
            ("Cdb", ctypes.c_ubyte * 16),
        ]

    HANDLE, DWORD, BOOL = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)

    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, DWORD, DWORD, ctypes.c_void_p, DWORD, DWORD, HANDLE]
    kernel32.CreateFileW.restype = HANDLE
    kernel32.DeviceIoControl.argtypes = [HANDLE, DWORD, ctypes.c_void_p, DWORD, ctypes.c_void_p, DWORD,
                                         ctypes.POINTER(DWORD), ctypes.c_void_p]
    kernel32.DeviceIoControl.restype = BOOL
    kernel32.CloseHandle.argtypes = [HANDLE]
    kernel32.CloseHandle.restype = BOOL

    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, DWORD]
    setupapi.SetupDiGetClassDevsW.restype = HANDLE
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), DWORD,
                                                     ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
    setupapi.SetupDiEnumDeviceInterfaces.restype = BOOL
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                          ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD),
                                                          ctypes.POINTER(SP_DEVINFO_DATA)]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [HANDLE]
    setupapi.SetupDiDestroyDeviceInfoList.restype = BOOL
    cfgmgr32.CM_Get_Device_IDW.argtypes = [DWORD, wintypes.LPWSTR, wintypes.ULONG, wintypes.ULONG]
    cfgmgr32.CM_Get_Device_IDW.restype = DWORD
    cfgmgr32.CM_Get_Parent.argtypes = [ctypes.POINTER(DWORD), DWORD, wintypes.ULONG]
    cfgmgr32.CM_Get_Parent.restype = DWORD
    cfgmgr32.CM_Locate_DevNodeW.argtypes = [ctypes.POINTER(DWORD), wintypes.LPCWSTR, wintypes.ULONG]
    cfgmgr32.CM_Locate_DevNodeW.restype = DWORD
    cfgmgr32.CM_Request_Device_EjectW.argtypes = [DWORD, ctypes.POINTER(ctypes.c_int), wintypes.LPWSTR,
                                                  wintypes.ULONG, wintypes.ULONG]
    cfgmgr32.CM_Request_Device_EjectW.restype = DWORD

    class Api:
        pass

    api = Api()
    api.ctypes, api.wintypes = ctypes, wintypes
    api.kernel32, api.setupapi, api.cfgmgr32 = kernel32, setupapi, cfgmgr32
    api.GUID, api.SP_DEVICE_INTERFACE_DATA, api.SP_DEVINFO_DATA = GUID, SP_DEVICE_INTERFACE_DATA, SP_DEVINFO_DATA
    api.SCSI_PASS_THROUGH_DIRECT = SCSI_PASS_THROUGH_DIRECT
    api.INVALID_HANDLE = ctypes.c_void_p(-1).value
    _api = api
    return api


def _guid(api, text: str):
    import uuid

    raw = uuid.UUID(text).bytes_le
    guid = api.GUID()
    api.ctypes.memmove(api.ctypes.byref(guid), raw, len(raw))
    return guid


@contextmanager
def _opened(path: str, access: int):
    """A CreateFileW handle (``None`` when the open fails), always closed afterwards."""
    api = _win32()
    handle = api.kernel32.CreateFileW(path, access, _FILE_SHARE_READ_WRITE, None, _OPEN_EXISTING, 0, None)
    if handle in (None, 0, api.INVALID_HANDLE):
        logger.debug("CreateFileW(%s) failed: error %d", path, api.ctypes.get_last_error())
        yield None
        return
    try:
        yield handle
    finally:
        api.kernel32.CloseHandle(handle)


def _ioctl(handle, code: int, in_bytes: bytes, out_size: int) -> bytes | None:
    api = _win32()
    in_buf = api.ctypes.create_string_buffer(in_bytes, len(in_bytes)) if in_bytes else None
    out_buf = api.ctypes.create_string_buffer(out_size)
    returned = api.wintypes.DWORD(0)
    ok = api.kernel32.DeviceIoControl(handle, code, in_buf, len(in_bytes), out_buf, out_size,
                                      api.ctypes.byref(returned), None)
    if not ok:
        logger.debug("DeviceIoControl(0x%08X) failed: error %d", code, api.ctypes.get_last_error())
        return None
    return out_buf.raw[:returned.value]


def _query_descriptor(handle) -> dict[str, Any]:
    query = struct.pack("<II4x", 0, 0)  # StorageDeviceProperty, PropertyStandardQuery
    header = _ioctl(handle, _IOCTL_STORAGE_QUERY_PROPERTY, query, 8)
    if not header or len(header) < 8:
        return {}
    size = max(struct.unpack_from("<I", header, 4)[0], 36)
    return parse_storage_descriptor(_ioctl(handle, _IOCTL_STORAGE_QUERY_PROPERTY, query, size) or b"")


def _device_number(handle) -> tuple[int, int] | None:
    return parse_device_number(_ioctl(handle, _IOCTL_STORAGE_GET_DEVICE_NUMBER, b"", 12) or b"")


def _instance_id(devinst: int) -> str:
    api = _win32()
    buffer = api.ctypes.create_unicode_buffer(512)
    if api.cfgmgr32.CM_Get_Device_IDW(devinst, buffer, len(buffer), 0) != _CR_SUCCESS:
        return ""
    return buffer.value


def _parent(devinst: int) -> int:
    api = _win32()
    parent = api.wintypes.DWORD(0)
    return parent.value if api.cfgmgr32.CM_Get_Parent(api.ctypes.byref(parent), devinst, 0) == _CR_SUCCESS else 0


def _disk_devinst(number: tuple[int, int]) -> int:
    """DevInst of the present disk interface whose device number matches the volume's."""
    api = _win32()
    ctypes = api.ctypes
    disk_guid = _guid(api, _GUID_DEVINTERFACE_DISK)
    devs = api.setupapi.SetupDiGetClassDevsW(ctypes.byref(disk_guid), None, None,
                                              _DIGCF_PRESENT | _DIGCF_DEVICEINTERFACE)
    if devs in (None, 0, api.INVALID_HANDLE):
        return 0
    detail_prefix = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6  # cbSize of SP_DEVICE_INTERFACE_DETAIL_DATA_W
    try:
        index = 0
        while True:
            iface = api.SP_DEVICE_INTERFACE_DATA(cbSize=ctypes.sizeof(api.SP_DEVICE_INTERFACE_DATA))
            if not api.setupapi.SetupDiEnumDeviceInterfaces(devs, None, ctypes.byref(disk_guid), index,
                                                            ctypes.byref(iface)):
                return 0
            index += 1
            needed = api.wintypes.DWORD(0)
            api.setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(iface), None, 0,
                                                          ctypes.byref(needed), None)
            if needed.value < 6:
                continue
            detail = ctypes.create_string_buffer(needed.value)
            struct.pack_into("<I", detail, 0, detail_prefix)
            devinfo = api.SP_DEVINFO_DATA(cbSize=ctypes.sizeof(api.SP_DEVINFO_DATA))
            if not api.setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(iface), detail, needed,
                                                                 None, ctypes.byref(devinfo)):
                continue
            path = ctypes.wstring_at(ctypes.addressof(detail) + 4)
            with _opened(path, 0) as disk:
                if disk is not None and _device_number(disk) == number:
                    return devinfo.DevInst
    finally:
        api.setupapi.SetupDiDestroyDeviceInfoList(devs)


def _pnp_ancestry(volume_handle) -> tuple[str, str, str]:
    """Instance ids of the volume's disk, its parent and grandparent (``""`` where unknown)."""
    number = _device_number(volume_handle)
    devinst = _disk_devinst(number) if number is not None else 0
    if not devinst:
        return "", "", ""
    parent = _parent(devinst)
    grandparent = _parent(parent) if parent else 0
    return _instance_id(devinst), _instance_id(parent) if parent else "", _instance_id(grandparent) if grandparent else ""


def disk_instance_id(mount_path: str) -> str:
    """PnP instance id of the disk behind *mount_path*'s drive letter (``""`` when unknown)."""
    letter = _drive_letter(mount_path)
    if sys.platform != "win32" or not letter:
        return ""
    try:
        with _opened(f"\\\\.\\{letter}:", 0) as volume:
            return _pnp_ancestry(volume)[0] if volume is not None else ""
    except (AttributeError, OSError) as exc:
        logger.debug("PnP lookup for %s: failed: %s", letter, exc)
        return ""


def request_device_eject(instance_id: str) -> tuple[int, int, str]:
    """Ask Windows to eject the device node: ``(CM error, veto type, veto name)``; ``(0, 0, "")`` is success."""
    api = _win32()
    node = api.wintypes.DWORD(0)
    error = api.cfgmgr32.CM_Locate_DevNodeW(api.ctypes.byref(node), instance_id, 0)
    if error != _CR_SUCCESS:
        return error, 0, ""
    veto_type = api.ctypes.c_int(0)
    veto_name = api.ctypes.create_unicode_buffer(260)
    error = api.cfgmgr32.CM_Request_Device_EjectW(node.value, api.ctypes.byref(veto_type), veto_name,
                                                  len(veto_name), 0)
    return error, veto_type.value, veto_name.value


# ── public probes ───────────────────────────────────────────────────


def storage_identity(mount_path: str) -> dict[str, Any]:
    """Identity of the Apple disk behind *mount_path*'s drive letter; ``{}`` when not an iPod.

    The GUID from the PnP tree overrides the descriptor's; the PID family and
    generation fill only fields the descriptor left empty.
    """
    if sys.platform != "win32":
        return {}
    letter = _drive_letter(mount_path)
    if not letter:
        return {}
    try:
        with _opened(f"\\\\.\\{letter}:", 0) as volume:  # query-only access needs no privileges
            if volume is None:
                return {}
            result = _descriptor_identity(_query_descriptor(volume))
            if not result:
                return {}
            for key, value in _tree_identity(*_pnp_ancestry(volume)).items():
                if key == "firewire_guid" or key not in result:
                    result[key] = value
    except (AttributeError, OSError) as exc:
        logger.debug("Storage IOCTL identity for %s: failed: %s", letter, exc)
        return {}
    logger.debug("Storage IOCTL identity for %s: %s", letter, result)
    return result


def usb_ids_from_registry(guid: str) -> tuple[int, int]:
    """``(vid, pid)`` of the Apple USB device whose instance name contains *guid*; ``(0, 0)`` if none."""
    wanted = str(guid or "").upper()
    if sys.platform != "win32" or not wanted:
        return 0, 0
    try:
        import winreg
    except ImportError:
        return 0, 0
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _USB_ENUM_KEY)
    except OSError:
        return 0, 0
    with root:
        for index in range(4096):
            try:
                name = winreg.EnumKey(root, index)
            except OSError:
                break
            upper = name.upper()
            if f"VID_{APPLE_VID:04X}" not in upper or "PID_" not in upper or "MI_" in upper:
                continue
            try:
                with winreg.OpenKey(root, name) as device_key:
                    instances = [winreg.EnumKey(device_key, i) for i in range(winreg.QueryInfoKey(device_key)[0])]
            except OSError:
                continue
            if any(wanted in instance.upper() for instance in instances):
                return usb_ids_from_instance_id(name)
    return 0, 0


def _make_inquiry(handle) -> InquiryFn:
    api = _win32()
    ctypes = api.ctypes

    def inquiry(evpd: bool, page: int, length: int) -> bytes | None:
        buffer = ctypes.create_string_buffer(length)
        sptd = api.SCSI_PASS_THROUGH_DIRECT()
        sptd.Length = ctypes.sizeof(sptd)
        cdb = inquiry_cdb(evpd, page, length)
        sptd.CdbLength = len(cdb)
        sptd.DataIn = _SCSI_IOCTL_DATA_IN
        sptd.DataTransferLength = length
        sptd.TimeOutValue = 10
        sptd.DataBuffer = ctypes.cast(buffer, ctypes.c_void_p)
        sptd.Cdb[:len(cdb)] = list(cdb)
        returned = api.wintypes.DWORD(0)
        ok = api.kernel32.DeviceIoControl(handle, _IOCTL_SCSI_PASS_THROUGH_DIRECT, ctypes.byref(sptd),
                                          ctypes.sizeof(sptd), ctypes.byref(sptd), ctypes.sizeof(sptd),
                                          ctypes.byref(returned), None)
        if not ok or sptd.ScsiStatus:
            return None
        return buffer.raw[:sptd.DataTransferLength or length]

    return inquiry


def query_ipod_vpd_for_path(mount_path: str, *, usb_pid: int = 0, serial_filter: str = "") -> dict[str, Any] | None:
    """Live SysInfoExtended of the iPod at *mount_path* over SCSI pass-through (Windows only)."""
    if sys.platform != "win32":
        return None
    letter = _drive_letter(mount_path)
    if not letter:
        return None
    try:
        with _opened(f"\\\\.\\{letter}:", _GENERIC_READ | _GENERIC_WRITE) as volume:
            if volume is None:
                return None
            return build_vpd_result(_make_inquiry(volume), source="windows_scsi",
                                    transport="windows_scsi_pass_through", usb_pid=usb_pid,
                                    serial_filter=serial_filter)
    except Exception as exc:
        logger.debug("Windows VPD query for %s: failed: %s", letter, exc)
        return None
