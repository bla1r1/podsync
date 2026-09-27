"""Transport-independent SCSI INQUIRY handling shared by every VPD probe.

iPods answer INQUIRY with vendor pages: 0xC0 lists the data pages, and the
pages from 0xC2 on carry consecutive fragments of the SysInfoExtended XML
(payload length in byte 3, payload from byte 4).  Page 0x80 is the standard
unit-serial page, which older iPods fill with the Apple product serial.

A transport only has to supply an :data:`InquiryFn`; everything else here is
pure byte handling and is exercised by tests with canned responses.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from podsync.hardware.catalog.sysinfo import normalize_guid, parse_sysinfo_extended

__all__ = [
    "APPLE_VID", "InquiryFn", "build_vpd_result", "inquiry_cdb", "page80_serial", "read_vpd_payload",
    "standard_inquiry_strings",
]

logger = logging.getLogger(__name__)

APPLE_VID = 0x05AC
InquiryFn = Callable[[bool, int, int], "bytes | None"]  # (evpd, page, allocation length) → data or None

_PAGE_LIST = 0xC0
_FIRST_DATA_PAGE = 0xC2
_UNIT_SERIAL_PAGE = 0x80
_STANDARD_INQUIRY_LENGTH = 96
_PAGE_LENGTH = 255


def inquiry_cdb(evpd: bool, page: int, length: int) -> bytes:
    """A 6-byte INQUIRY command block."""
    return bytes([0x12, 0x01 if evpd else 0x00, page & 0xFF, (length >> 8) & 0xFF, length & 0xFF, 0])


def _ask(inquiry: InquiryFn, evpd: bool, page: int, length: int) -> bytes:
    try:
        return inquiry(evpd, page, length) or b""
    except Exception as exc:  # one failing page must not abort the whole read
        logger.debug("INQUIRY evpd=%s page=0x%02X failed: %s", evpd, page, exc)
        return b""


def _page_payload(data: bytes) -> bytes:
    return data[4:4 + data[3]] if len(data) >= 4 else b""


def read_vpd_payload(inquiry: InquiryFn) -> bytes:
    """The SysInfoExtended bytes spread over the vendor pages, trailing NULs removed."""
    listing = _ask(inquiry, True, _PAGE_LIST, _PAGE_LENGTH)
    pages = [page for page in _page_payload(listing) if page >= _FIRST_DATA_PAGE] if len(listing) > 4 else []
    if not pages:
        pages = list(range(_FIRST_DATA_PAGE, 0x100))
    fragments = (_page_payload(_ask(inquiry, True, page, _PAGE_LENGTH)) for page in pages)
    return b"".join(fragment for fragment in fragments if any(fragment)).rstrip(b"\x00")


def standard_inquiry_strings(inquiry: InquiryFn) -> dict[str, str]:
    """Vendor, product and revision from the standard INQUIRY reply."""
    data = _ask(inquiry, False, 0, _STANDARD_INQUIRY_LENGTH)
    if len(data) < 36:
        return {}

    def text(start: int, end: int) -> str:
        return data[start:end].decode("ascii", errors="replace").strip()

    return {"scsi_vendor": text(8, 16), "scsi_product": text(16, 32), "scsi_revision": text(32, 36)}


def page80_serial(inquiry: InquiryFn) -> str:
    """Unit serial from page 0x80: the text before the first NUL, trimmed."""
    payload = _page_payload(_ask(inquiry, True, _UNIT_SERIAL_PAGE, _PAGE_LENGTH))
    return payload.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()


def build_vpd_result(inquiry: InquiryFn, *, source: str, transport: str, usb_pid: int = 0, serial_filter: str = "",
                     extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Everything one device tells over INQUIRY, or ``None`` when it is not the iPod asked for.

    The result holds the parsed plist keys, the standard INQUIRY strings,
    ``vpd_serial`` (page 0x80) and ``vpd_raw_xml``.  With *serial_filter*, a
    device whose GUID (``FireWireGUID``, else ``usb_serial``, else the page
    0x80 serial) differs is rejected.
    """
    payload = read_vpd_payload(inquiry)
    if not payload:
        return None
    parsed = parse_sysinfo_extended(payload, source=source, live=True)
    if not parsed.plist:
        return None
    result: dict[str, Any] = {"_source": source, "_transport": transport}
    if usb_pid:
        result.update(usb_vid=APPLE_VID, usb_pid=usb_pid)
    result.update(standard_inquiry_strings(inquiry))
    if serial := page80_serial(inquiry):
        result["vpd_serial"] = serial
    result["vpd_raw_xml"] = parsed.raw_xml
    result.update(parsed.plist)
    result.update(extra or {})

    wanted = normalize_guid(serial_filter)
    found = normalize_guid(result.get("FireWireGUID") or result.get("usb_serial") or result.get("vpd_serial"))
    if wanted and found and wanted != found:
        logger.debug("VPD reply from %s belongs to %s, not %s", transport, found, wanted)
        return None
    return result
