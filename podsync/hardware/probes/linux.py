"""Live SysInfoExtended on Linux: SCSI INQUIRY through the SG_IO ioctl.

The whole-disk node is tried before the partition, since some kernels only
pass SCSI commands through on the disk.  Reading usually needs membership of
the ``disk`` group (or the udev rule from :mod:`podsync.hardware.linux.udev`).
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from typing import Any

from podsync.hardware.linux.identity import find_block_device, whole_disk_device
from podsync.hardware.probes.scsi import InquiryFn, build_vpd_result, inquiry_cdb

__all__ = ["query_ipod_vpd_for_path"]

logger = logging.getLogger(__name__)

_SG_IO = 0x2285
_SG_DXFER_FROM_DEV = -3
_SENSE_LENGTH = 32
_TIMEOUT_MS = 10_000


class _SgIoHdr(ctypes.Structure):
    """``struct sg_io_hdr`` from ``<scsi/sg.h>``."""

    _fields_ = [
        ("interface_id", ctypes.c_int),
        ("dxfer_direction", ctypes.c_int),
        ("cmd_len", ctypes.c_ubyte),
        ("mx_sb_len", ctypes.c_ubyte),
        ("iovec_count", ctypes.c_ushort),
        ("dxfer_len", ctypes.c_uint),
        ("dxferp", ctypes.c_void_p),
        ("cmdp", ctypes.c_void_p),
        ("sbp", ctypes.c_void_p),
        ("timeout", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("pack_id", ctypes.c_int),
        ("usr_ptr", ctypes.c_void_p),
        ("status", ctypes.c_ubyte),
        ("masked_status", ctypes.c_ubyte),
        ("msg_status", ctypes.c_ubyte),
        ("sb_len_wr", ctypes.c_ubyte),
        ("host_status", ctypes.c_ushort),
        ("driver_status", ctypes.c_ushort),
        ("resid", ctypes.c_int),
        ("duration", ctypes.c_uint),
        ("info", ctypes.c_uint),
    ]


def _block_candidates(mount_path: str) -> list[str]:
    """Whole disk first, then the partition itself (``/dev/sdf1`` → ``/dev/sdf``, ``/dev/sdf1``)."""
    try:
        device = find_block_device(mount_path)
    except Exception:
        return []
    if not device:
        return []
    return list(dict.fromkeys(node for node in (whole_disk_device(device), device) if node))


def _make_inquiry(fd: int) -> InquiryFn:
    import fcntl

    def inquiry(evpd: bool, page: int, length: int) -> bytes | None:
        cdb = ctypes.create_string_buffer(inquiry_cdb(evpd, page, length), 6)
        data = ctypes.create_string_buffer(length)
        sense = ctypes.create_string_buffer(_SENSE_LENGTH)
        header = _SgIoHdr(
            interface_id=ord("S"), dxfer_direction=_SG_DXFER_FROM_DEV, cmd_len=6, mx_sb_len=_SENSE_LENGTH,
            dxfer_len=length, timeout=_TIMEOUT_MS,
        )
        header.dxferp = ctypes.cast(data, ctypes.c_void_p)
        header.cmdp = ctypes.cast(cdb, ctypes.c_void_p)
        header.sbp = ctypes.cast(sense, ctypes.c_void_p)
        fcntl.ioctl(fd, _SG_IO, header)
        if header.status or header.host_status:
            return None
        return data.raw[:max(0, length - header.resid)]

    return inquiry


def query_ipod_vpd_for_path(mount_path: str, *, usb_pid: int = 0, serial_filter: str = "") -> dict[str, Any] | None:
    """SysInfoExtended and INQUIRY strings of the iPod mounted at *mount_path* (Linux only)."""
    if not sys.platform.startswith("linux"):
        return None
    for candidate in _block_candidates(mount_path):
        fd = -1
        try:
            fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            result = build_vpd_result(
                _make_inquiry(fd), source="linux_scsi", transport="linux_sg_io_scsi_vpd", usb_pid=usb_pid,
                serial_filter=serial_filter, extra={"block_device": candidate},
            )
            if result:
                return result
        except PermissionError as exc:
            logger.info("Permission denied reading VPD from %s: %s", candidate, exc)
        except Exception as exc:
            logger.info("VPD query failed for %s: %s", candidate, exc)
        finally:
            if fd >= 0:
                os.close(fd)
    return None
