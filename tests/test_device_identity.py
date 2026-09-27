"""Device identity probes, guarded metadata writes, USB backend, VPD CLI.

Dropped non-hermetic cases are recorded at the bottom of this file.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from podsync.hardware.linux import identity as linux_identity
from podsync.hardware.linux import udev as linux_integration
from podsync.hardware.probes import backend as usb_backend
from podsync.hardware.probes import libusb as libusb_probe
from podsync.hardware.probes import linux as linux_probe
from podsync.hardware.virtual.device import make_virtual_ipod
from podsync.itdb.writer.signing.aes72 import write_hash_info


# ---------------------------------------------------------------------------
# Shared construction helpers
# ---------------------------------------------------------------------------


def _bare_virtual_root(root: Path) -> None:
    (root / "iPod_Control" / "iTunes").mkdir(parents=True)
    (root / "iPodInfo.json").write_text("{}", encoding="utf-8")


def _fake_run(monkeypatch, text: str) -> None:
    """Answer every identity subprocess call with one canned stdout block."""
    monkeypatch.setattr(
        linux_identity.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=text),
    )


def _silence_other_sources(monkeypatch) -> None:
    """Leave sysfs and the USB bus out of an identity probe under test."""
    monkeypatch.setattr(
        linux_identity,
        "_identity_from_sysfs",
        lambda _disk: {},
    )
    monkeypatch.setattr(
        linux_identity,
        "_identity_from_usb_bus",
        lambda _disk: {},
    )


# ---------------------------------------------------------------------------
# Guarded device metadata writes
# ---------------------------------------------------------------------------


class TestGuardedMetadataWrites:
    def test_sysinfo_write_targets_guarded_device_metadata(self, tmp_path: Path):
        _bare_virtual_root(tmp_path)

        ok = libusb_probe.write_sysinfo(
            str(tmp_path),
            {
                "SerialNumber": "A9X8Y7W6V5",
                "FireWireGUID": "0F1E2D3C4B5A6978",
                "ModelNumStr": "xC442",
                "vpd_raw_xml": b"junk<plist><dict><key>k</key></dict></plist>",
            },
        )

        meta_dir = tmp_path / "iPod_Control" / "Device"
        assert ok is True
        assert (meta_dir / "SysInfo").read_text(encoding="utf-8") == (
            "pszSerialNumber: A9X8Y7W6V5\n"
            "FirewireGuid: 0x0F1E2D3C4B5A6978\n"
            "ModelNumStr: xC442\n"
        )
        assert (meta_dir / "SysInfoExtended").read_bytes().startswith(b"<plist>")

    def test_hash_info_write_targets_guarded_device_metadata(self, tmp_path: Path):
        _bare_virtual_root(tmp_path)

        uuid_blob = bytes(range(0x10, 0x24))
        iv_blob = bytes(range(0x40, 0x50))
        rnd_blob = bytes(range(0x60, 0x6C))

        ok = write_hash_info(str(tmp_path), uuid_blob, iv_blob, rnd_blob)

        blob = (tmp_path / "iPod_Control" / "Device" / "HashInfo").read_bytes()
        assert ok is True
        assert blob == b"HASHv0" + uuid_blob + rnd_blob + iv_blob
        assert len(blob) == 54


# ---------------------------------------------------------------------------
# VPD reporting paths
# ---------------------------------------------------------------------------


class TestVpdReporting:
    def test_vpd_cli_reports_identity_and_records_one_write(
        self,
        monkeypatch,
        capsys,
        tmp_path: Path,
    ):
        _bare_virtual_root(tmp_path)

        record = {
            "usb_pid": 0x1201,
            "SerialNumber": "MKTOPP2C7",
            "FireWireGUID": "0F1E2D3C4B5A6978",
            "VisibleBuildID": "1.53",
            "FamilyID": 1,
            "vpd_raw_xml": b"<?xml version='1.0'?><plist><dict></dict></plist>",
        }
        logged = []
        monkeypatch.setattr(libusb_probe, "query_all_ipods", lambda: [record])
        monkeypatch.setattr(
            libusb_probe,
            "write_sysinfo",
            lambda path, payload: logged.append((path, payload)) or True,
        )
        monkeypatch.setattr(libusb_probe.sys, "platform", "win32")
        monkeypatch.setattr(
            sys,
            "argv",
            ["vpd_libusb", "--write-sysinfo", "--path", str(tmp_path)],
        )

        root_log = logging.getLogger()
        held_handlers, held_level = root_log.handlers[:], root_log.level
        try:
            code = libusb_probe.main()
        finally:
            root_log.handlers[:] = held_handlers
            root_log.setLevel(held_level)

        printed = capsys.readouterr().out
        assert code == 0
        assert len(logged) == 1
        assert logged[0][0] == str(tmp_path)
        assert logged[0][1] is record
        assert "Apple Serial:    MKTOPP2C7" in printed
        assert "Model Number:    MB565" in printed
        assert "iPod (USB PID 0x1201)" in printed
        assert "Done!" in printed
        assert str(tmp_path) in printed

    def test_page_80_live_serial_feeds_model_lookup(self, monkeypatch):
        """Older players expose their Apple serial on VPD page 0x80."""

        iokit_stub = ModuleType("podsync.hardware.probes.macos")
        monkeypatch.setattr(
            iokit_stub,
            "query_ipod_vpd",
            lambda **_kwargs: {
                "vpd_serial": "7H31200NNRH",
                "usb_pid": 0x1201,
            },
            raising=False,
        )
        control_stub = ModuleType("podsync.hardware.probes.usb_control")
        monkeypatch.setattr(
            control_stub,
            "query_ipod_usb_sysinfo_extended",
            lambda **_kwargs: None,
            raising=False,
        )
        monkeypatch.setattr(libusb_probe.sys, "platform", "darwin")
        monkeypatch.setitem(sys.modules, "podsync.hardware.probes.macos", iokit_stub)
        monkeypatch.setitem(
            sys.modules,
            "podsync.hardware.probes.usb_control",
            control_stub,
        )

        found = libusb_probe.identify_by_vpd(
            mount_path="",
            usb_pid=0x1201,
            write_sysinfo_to_device=False,
        )

        assert found is not None
        assert found["serial"] == "7H31200NNRH"
        assert found["model_number"] == "M8976"

    def test_vpd_candidate_order_prefers_whole_disk(self, monkeypatch):
        monkeypatch.setattr(
            linux_probe,
            "find_block_device",
            lambda _mount: "/dev/sdf1",
        )

        assert linux_probe._block_candidates("/media/wendy/POD") == [
            "/dev/sdf",
            "/dev/sdf1",
        ]

    def test_cached_page_80_payload_parses_product_serial(self):
        blob = b"7H31200NNRH"
        head = bytes((0x00, 0x80)) + len(blob).to_bytes(2, "big") + blob

        assert linux_identity.parse_vpd_page_80(head) == "7H31200NNRH"


# ---------------------------------------------------------------------------
# Packaged USB backend resolution
# ---------------------------------------------------------------------------


class TestUsbBackendResolution:
    def test_packaged_libusb_lookup_supplies_pyusb_backend(
        self,
        monkeypatch,
        tmp_path: Path,
    ):
        bundled = tmp_path / "libusb-1.0.dylib"
        bundled.touch()
        beacon = object()
        trace: dict = {}

        def compose(*, find_library=None):
            if find_library is None:
                return None
            trace["path"] = find_library("usb-1.0")
            return beacon

        libusb1 = ModuleType("usb.backend.libusb1")
        libusb1.get_backend = compose
        backend_pkg = ModuleType("usb.backend")
        backend_pkg.libusb1 = libusb1
        backend_pkg.__path__ = []
        usb_pkg = ModuleType("usb")
        usb_pkg.backend = backend_pkg
        usb_pkg.__path__ = []
        finder = ModuleType("libusb_package")
        finder.find_library = lambda: str(bundled)

        monkeypatch.setitem(sys.modules, "usb", usb_pkg)
        monkeypatch.setitem(sys.modules, "usb.backend", backend_pkg)
        monkeypatch.setitem(sys.modules, "usb.backend.libusb1", libusb1)
        monkeypatch.setitem(sys.modules, "libusb_package", finder)
        monkeypatch.setattr(usb_backend.sys, "platform", "darwin")
        monkeypatch.setattr(
            usb_backend.ctypes.util,
            "find_library",
            lambda _name: None,
        )
        monkeypatch.delenv("PODSYNC_LIBUSB_DLL", raising=False)
        monkeypatch.delenv("PYUSB_LIBUSB_DLL", raising=False)

        resolved = usb_backend.get_libusb_backend()

        assert resolved is beacon
        assert trace["path"] == str(bundled)


# ---------------------------------------------------------------------------
# udev and probe evidence
# ---------------------------------------------------------------------------


class TestUdevEvidence:
    def test_namespaced_serial_is_read_from_host_udev_database(
        self,
        monkeypatch,
        tmp_path: Path,
    ):
        rules_dump = tmp_path / "udev-data"
        rules_dump.mkdir()
        (rules_dump / "b8:208").write_text(
            "I:998877\n"
            "E:ID_PODSYNC_RULE_VERSION=2\n"
            "E:ID_PODSYNC_PRODUCT_SERIAL=9Q551288Z3R\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(linux_identity, "_UDEV_DATA_DIRECTORY", rules_dump)
        monkeypatch.setattr(
            linux_identity,
            "_udev_database_key",
            lambda _device: "b8:208",
        )

        def _no_subprocess(*_args, **_kwargs):
            raise FileNotFoundError

        monkeypatch.setattr(linux_identity.subprocess, "run", _no_subprocess)

        found = linux_identity._identity_from_udev("/dev/sdb")

        assert found == {
            "serial": "9Q551288Z3R",
            "_sources": {"serial": "udev_scsi_id"},
        }

    def test_namespaced_serial_stands_alone_or_beside_transport_fields(
        self,
        monkeypatch,
    ):
        _fake_run(
            monkeypatch,
            "ID_VENDOR_ID=05ac\n"
            "ID_MODEL_ID=1261\n"
            "ID_SERIAL_SHORT=01A2B3C4D5E6F708\n"
            "ID_PODSYNC_PRODUCT_SERIAL=9Q551288Z3R\n",
        )

        rich = linux_identity._identity_from_udev("/dev/sdg")

        assert rich["serial"] == "9Q551288Z3R"
        assert rich["firewire_guid"] == "01A2B3C4D5E6F708"
        assert rich["usb_pid"] == 0x1261
        assert rich["_sources"] == {
            "serial": "udev_scsi_id",
            "firewire_guid": "udev",
            "usb_pid": "udev",
        }

        _fake_run(
            monkeypatch,
            "ID_PODSYNC_PRODUCT_SERIAL=9Q551288Z3R\n",
        )

        bare = linux_identity._identity_from_udev("/dev/sdg")

        assert bare == {
            "serial": "9Q551288Z3R",
            "_sources": {"serial": "udev_scsi_id"},
        }

    def test_probe_reads_serial_from_mount_anchored_by_id_link(
        self,
        monkeypatch,
        tmp_path: Path,
    ):
        mount = tmp_path / "ipod"
        make_virtual_ipod(mount, "MB565")
        links_dir = tmp_path / "dev" / "disk" / "by-id"
        links_dir.mkdir(parents=True)
        matched = links_dir / "ipod-D3V1CE0007A1"
        decoy = links_dir / "ipod-WRONGUNIT"
        matched.touch()
        decoy.touch()

        monkeypatch.setattr(
            linux_identity,
            "find_block_device",
            lambda _mount: "/dev/sdb1",
        )
        monkeypatch.setattr(linux_identity, "_BY_ID_DIRECTORY", links_dir)
        monkeypatch.setattr(linux_identity, "_identity_from_udev", lambda _device: {})
        _silence_other_sources(monkeypatch)

        native = linux_identity.os.path.realpath

        def resolve_path(path) -> str:
            text = str(path)
            if text == str(matched):
                return "/dev/sdb"
            if text == str(decoy):
                return "/dev/sdc"
            if text in {"/dev/sdb", "/dev/sdb1"}:
                return text
            return native(path)

        monkeypatch.setattr(linux_identity.os.path, "realpath", resolve_path)

        found = linux_identity.linux_device_identity(str(mount))

        assert found["serial"] == "D3V1CE0007A1"
        assert found["_sources"]["serial"] == "udev_scsi_id"

    def test_probe_merges_partition_transport_with_disk_serial(self, monkeypatch):
        monkeypatch.setattr(
            linux_identity,
            "find_block_device",
            lambda _mount: "/dev/sdg1",
        )

        def partition_then_disk(device: str) -> dict:
            if device == "/dev/sdg1":
                return {
                    "firewire_guid": "01A2B3C4D5E6F708",
                    "usb_pid": 0x1261,
                    "_sources": {
                        "firewire_guid": "udev",
                        "usb_pid": "udev",
                    },
                }
            assert device == "/dev/sdg"
            return {
                "serial": "9Q551288Z3R",
                "_sources": {"serial": "udev_scsi_id"},
            }

        monkeypatch.setattr(
            linux_identity,
            "_identity_from_udev",
            partition_then_disk,
        )
        _silence_other_sources(monkeypatch)

        merged = linux_identity.linux_device_identity("/media/wendy/POD")

        assert merged["serial"] == "9Q551288Z3R"
        assert merged["firewire_guid"] == "01A2B3C4D5E6F708"
        assert merged["usb_pid"] == 0x1261
        assert merged["_sources"] == {
            "serial": "udev_scsi_id",
            "firewire_guid": "udev",
            "usb_pid": "udev",
        }


# ---------------------------------------------------------------------------
# Setup prompt gate
# ---------------------------------------------------------------------------


class TestSetupGate:
    def test_setup_prompt_gated_on_unresolved_identity(self, tmp_path: Path):
        gadget = make_virtual_ipod(tmp_path, "MB029")
        gadget.model_number = ""
        gadget.serial = ""

        assert linux_integration.udev_rule_needed(
            gadget,
            platform="linux",
        )

        gadget.model_number = "MB029"
        assert linux_integration.udev_rule_needed(
            gadget,
            platform="linux",
        )
        gadget.serial = "R7K2209TF3M"
        assert not linux_integration.udev_rule_needed(
            gadget,
            platform="linux",
        )
        gadget.serial = ""
        assert not linux_integration.udev_rule_needed(
            gadget,
            platform="win32",
        )

        transport_less = SimpleNamespace(
            path=str(tmp_path),
            model_number="",
            serial="",
            usb_pid=0,
            firewire_guid="",
        )

        assert linux_integration.udev_rule_needed(
            transport_less,
            platform="linux",
        )


# ---------------------------------------------------------------------------
# Host-side discovery helpers
# ---------------------------------------------------------------------------


class TestHostDiscovery:
    def test_sysfs_path_blocks_uncached_serial_for_non_ipods(self, monkeypatch):
        noted: list[str] = []
        monkeypatch.setattr(
            linux_identity,
            "_is_ipod_scsi_device",
            lambda _disk: False,
        )
        monkeypatch.setattr(linux_identity.os.path, "exists", lambda _path: False)
        monkeypatch.setattr(
            linux_identity,
            "_cached_product_serial",
            lambda disk: noted.append(disk) or "CACHEDVALUE",
        )

        assert linux_identity._identity_from_sysfs("sdb") == {}
        assert noted == []

    def test_usb_identity_filter_accepts_ipods_rejects_iphone(self):
        verdicts = [
            ((0x1261, ""), True),
            ((None, "iPod"), True),
            ((0x12A8, "iPhone"), False),
        ]

        for (pid, product), wanted in verdicts:
            assert bool(linux_identity._is_ipod_usb_identity(pid, product)) is wanted

    def test_findmnt_bind_style_source_reduces_to_device(self, monkeypatch):
        _fake_run(monkeypatch, "/dev/sdf1[iPod_Control/Music]\n")

        assert linux_identity.find_block_device("/media/wendy/POD") == "/dev/sdf1"
