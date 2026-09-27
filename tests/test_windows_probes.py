"""Windows storage IOCTL parsing, SCSI VPD page handling and firmware plist repair.

Everything here is hermetic: the Win32 calls are replaced by canned byte
strings, so the tests run on any platform.
"""

from __future__ import annotations

import struct

import pytest

from podsync.hardware.catalog.sysinfo import parse_sysinfo_extended
from podsync.hardware.discovery import windows as windows_discovery
from podsync.hardware.probes import scsi
from podsync.hardware.probes import windows as windows_probe

# ---------------------------------------------------------------------------
# Canned device replies
# ---------------------------------------------------------------------------

# iPod 5G/5.5G firmware lists image specs as <array><key>id</key><dict>…,
# which is not a valid plist.
_BROKEN_ARRAY_PLIST = b"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
<key>SerialNumber</key>
<string>4H6120AVV9M</string>
<key>FireWireGUID</key>
<string>000A27001C2D3E4F</string>
<key>AlbumArt</key>
<array>
<key>1028</key>
<dict>
<key>FormatId</key><integer>1028</integer>
<key>RenderWidth</key><integer>100</integer>
<key>RenderHeight</key><integer>100</integer>
</dict>
<key>1029</key>
<dict>
<key>FormatId</key><integer>1029</integer>
<key>RenderWidth</key><integer>200</integer>
<key>RenderHeight</key><integer>200</integer>
</dict>
</array>
<key>PodcastsSupported</key>
<true/>
</dict>
</plist>"""


def _storage_descriptor(vendor: bytes, product: bytes, revision: bytes, serial: bytes) -> bytes:
    """A STORAGE_DEVICE_DESCRIPTOR with its strings packed after the fixed part."""
    layout = "<IIBBBB5II"  # Version, Size, type/modifier/removable/queueing, 4 string offsets + BusType, raw length
    fixed = struct.calcsize(layout)
    strings = [vendor, product, revision, serial]
    offsets, blob = [], b""
    for text in strings:
        offsets.append(fixed + len(blob))
        blob += text + b"\x00"
    head = struct.pack(layout, 1, fixed + len(blob), 0, 0, 1, 0, *offsets, 7, 0)
    return head + blob


def _vpd_device(xml: bytes, *, serial: bytes = b"") -> scsi.InquiryFn:
    """An INQUIRY responder that serves *xml* over pages 0xC2… in 250-byte fragments."""
    fragments = [xml[i:i + 250] for i in range(0, len(xml), 250)]
    pages = {0xC2 + n: fragment for n, fragment in enumerate(fragments)}

    def inquiry(evpd: bool, page: int, length: int) -> bytes | None:
        if not evpd:
            return b"\x00" * 8 + b"Apple   " + b"iPod            " + b"1.62" + b"\x00" * 60
        if page == 0xC0:
            listing = bytes(sorted(pages))
            return bytes((0, 0xC0, 0, len(listing))) + listing
        if page == 0x80:
            return bytes((0, 0x80, 0, len(serial))) + serial
        if page in pages:
            return bytes((0, page, 0, len(pages[page]))) + pages[page]
        return None

    return inquiry


# ---------------------------------------------------------------------------
# SysInfoExtended repair
# ---------------------------------------------------------------------------


class TestFirmwarePlistRepair:
    def test_keys_inside_arrays_are_dropped_and_formats_survive(self):
        parsed = parse_sysinfo_extended(_BROKEN_ARRAY_PLIST)

        assert parsed.repaired is True
        assert parsed.used_regex_fallback is False
        assert parsed.cover_art_formats == {1028: (100, 100), 1029: (200, 200)}
        assert parsed.plist["PodcastsSupported"] is True

    def test_raw_xml_keeps_the_device_bytes(self):
        parsed = parse_sysinfo_extended(_BROKEN_ARRAY_PLIST)

        assert parsed.raw_xml == _BROKEN_ARRAY_PLIST

    def test_valid_plist_is_not_marked_repaired(self):
        valid = b"<?xml version='1.0'?><plist version='1.0'><dict><key>A</key><integer>1</integer></dict></plist>"

        parsed = parse_sysinfo_extended(valid)

        assert parsed.repaired is False
        assert parsed.plist == {"A": 1}

    def test_truncated_broken_plist_is_closed_then_repaired(self):
        cut = _BROKEN_ARRAY_PLIST[:_BROKEN_ARRAY_PLIST.index(b"<key>PodcastsSupported")]

        parsed = parse_sysinfo_extended(cut)

        assert parsed.repaired is True
        assert parsed.cover_art_formats == {1028: (100, 100), 1029: (200, 200)}


# ---------------------------------------------------------------------------
# SCSI VPD pages
# ---------------------------------------------------------------------------


class TestScsiVpd:
    def test_fragments_are_joined_across_pages(self):
        assert scsi.read_vpd_payload(_vpd_device(_BROKEN_ARRAY_PLIST)) == _BROKEN_ARRAY_PLIST

    def test_result_carries_inquiry_strings_serial_and_plist(self):
        result = scsi.build_vpd_result(
            _vpd_device(_BROKEN_ARRAY_PLIST, serial=b"4H6120AVV9M\x00"),
            source="windows_scsi", transport="windows_scsi_pass_through", usb_pid=0x1209,
        )

        assert result["scsi_vendor"] == "Apple"
        assert result["scsi_revision"] == "1.62"
        assert result["vpd_serial"] == "4H6120AVV9M"
        assert result["usb_pid"] == 0x1209
        assert result["SerialNumber"] == "4H6120AVV9M"
        assert isinstance(result["AlbumArt"], list)

    def test_other_device_is_rejected_by_guid_filter(self):
        result = scsi.build_vpd_result(
            _vpd_device(_BROKEN_ARRAY_PLIST), source="linux_scsi", transport="t",
            serial_filter="000A270099999999",
        )

        assert result is None

    def test_failing_pages_are_skipped(self):
        healthy = _vpd_device(_BROKEN_ARRAY_PLIST)

        def flaky(evpd: bool, page: int, length: int) -> bytes | None:
            if page == 0xC0:
                raise OSError("device busy")
            return healthy(evpd, page, length)

        assert scsi.read_vpd_payload(flaky) == _BROKEN_ARRAY_PLIST


# ---------------------------------------------------------------------------
# Storage descriptor and PnP instance ids
# ---------------------------------------------------------------------------


class TestStorageDescriptor:
    def test_descriptor_strings_are_read_at_their_offsets(self):
        parsed = windows_probe.parse_storage_descriptor(
            _storage_descriptor(b"Apple", b"iPod", b"1.62", b"000A27001C2D3E4F"),
        )

        assert parsed["vendor"] == "Apple"
        assert parsed["product"] == "iPod"
        assert parsed["revision"] == "1.62"
        assert parsed["serial"] == "000A27001C2D3E4F"
        assert parsed["removable"] is True
        assert parsed["bus_type"] == 7

    def test_short_buffer_gives_nothing(self):
        assert windows_probe.parse_storage_descriptor(b"\x00" * 12) == {}

    def test_hex_serial_becomes_guid_and_other_serial_stays_serial(self):
        as_guid = windows_probe._descriptor_identity(
            {"vendor": "Apple", "revision": "1.3", "serial": "000a 2700 1c2d 3e4f"})
        as_serial = windows_probe._descriptor_identity(
            {"vendor": "APPLE INC.", "revision": "1.3", "serial": "4H6120AVV9M"})

        assert as_guid["firewire_guid"] == "000A27001C2D3E4F"
        assert "serial" not in as_guid
        assert as_serial["serial"] == "4H6120AVV9M"
        assert as_serial["firmware"] == "1.3"

    def test_foreign_vendor_is_ignored(self):
        assert windows_probe._descriptor_identity({"vendor": "SanDisk", "serial": "1234"}) == {}

    def test_instance_ids_give_guid_vid_and_pid(self):
        disk = r"USBSTOR\DISK&VEN_APPLE&PROD_IPOD&REV_1.62\000A27001C2D3E4F&0"
        parent = r"USB\VID_05AC&PID_1209\000A27001C2D3E4F"

        tree = windows_probe._tree_identity(disk, parent, "")

        assert tree["firewire_guid"] == "000A27001C2D3E4F"
        assert (tree["usb_vid"], tree["usb_pid"]) == (0x05AC, 0x1209)
        assert tree["model_family"] == "iPod"
        assert "generation" not in tree  # 0x1209 is shared by the 5G and 5.5G

    def test_composite_parent_takes_guid_from_grandparent(self):
        disk = r"USBSTOR\DISK&VEN_APPLE&PROD_IPOD\7&1A2B3C&0"
        parent = r"USB\VID_05AC&PID_1261&MI_00\8&2B3C4D&0&0000"
        grandparent = r"USB\VID_05AC&PID_1261\000A27002A3B4C5D"

        tree = windows_probe._tree_identity(disk, parent, grandparent)

        assert tree["firewire_guid"] == "000A27002A3B4C5D"
        assert tree["usb_grandparent_instance_id"] == grandparent
        assert tree["usb_pid"] == 0x1261


# ---------------------------------------------------------------------------
# Scanner fallback order on Windows
# ---------------------------------------------------------------------------


class TestWindowsDiscovery:
    def test_ioctl_answer_skips_the_management_service(self, monkeypatch):
        monkeypatch.setattr(windows_probe, "storage_identity", lambda _root: {"serial": "4H6120AVV9M"})
        monkeypatch.setattr(windows_discovery, "_wmi_disk_line", lambda _letter: pytest.fail("WMI was queried"))

        assert windows_discovery.probe_windows_hardware("E:") == ({"serial": "4H6120AVV9M"}, "ioctl")

    def test_wmi_fallback_finds_pid_in_registry(self, monkeypatch):
        monkeypatch.setattr(windows_probe, "storage_identity", lambda _root: {})
        monkeypatch.setattr(
            windows_discovery, "_wmi_disk_line",
            lambda _letter: "USBSTOR\\DISK&VEN_APPLE&PROD_IPOD&REV_1.62\\000A27001C2D3E4F&0\t4H6120AVV9M\tApple iPod",
        )
        monkeypatch.setattr(windows_probe, "usb_ids_from_registry",
                            lambda guid: (0x05AC, 0x1209) if guid == "000A27001C2D3E4F" else (0, 0))

        found, method = windows_discovery.probe_windows_hardware("E:")

        assert method == "wmi"
        assert found["serial"] == "4H6120AVV9M"
        assert found["firmware"] == "1.62"
        assert found["firewire_guid"] == "000A27001C2D3E4F"
        assert found["usb_pid"] == 0x1209
        assert found["model_family"] == "iPod"

    def test_no_drive_letter_gives_nothing(self):
        assert windows_discovery.probe_windows_hardware("") == ({}, "ioctl")
