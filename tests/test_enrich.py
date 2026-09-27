"""Identity consistency repairs and late fill-ins done by ``enrich``.

Devices here carry pre-recorded hardware and VPD evidence so ``enrich`` never
probes the machine running the tests.
"""

from __future__ import annotations

import struct
from pathlib import Path

from podsync.hardware.catalog.models import IPOD_MODELS
from podsync.hardware.current import IpodDevice
from podsync.hardware.enrich import enrich


def _offline_device(**fields) -> IpodDevice:
    device = IpodDevice(**fields)
    device.raw_identity_evidence = {"hardware": [{}], "live_vpd": []}
    return device


def _with_sources(device: IpodDevice, **sources: str) -> IpodDevice:
    device._field_sources.update(sources)
    return device


class TestUsbPidAnchor:
    def test_cached_model_contradicting_the_live_pid_is_dropped(self):
        device = _with_sources(
            _offline_device(model_number="MB029", usb_pid=0x1224),  # SysInfo says Classic, PID says nano 3G
            model_number="sysinfo", usb_pid="device_tree",
        )

        enrich(device)

        assert device.model_number == ""
        assert (device.model_family, device.generation) == ("iPod Nano", "3rd Gen")
        assert device._field_sources["model_family"] == "usb_pid"
        assert device.capacity != "80GB"  # no nano 3G was ever sold with 80 GB

    def test_agreeing_pid_keeps_the_model(self):
        device = _with_sources(_offline_device(model_number="MB147", usb_pid=0x1241),
                               model_number="sysinfo", usb_pid="device_tree")

        enrich(device)

        assert device.model_number == "MB147"
        assert (device.model_family, device.generation, device.capacity, device.color) == (
            "iPod Classic", "6th Gen", "80GB", "Black")


class TestImpossibleVariants:
    def test_color_the_generation_never_had_is_cleared(self):
        device = _with_sources(
            _offline_device(model_family="iPod Mini", generation="2nd Gen", capacity="6GB", color="Gold"),
            model_family="sysinfo", generation="sysinfo", capacity="sysinfo", color="sysinfo",
        )

        enrich(device)

        assert device.color == ""
        assert device.capacity == "6GB"

    def test_mismatched_pair_loses_the_weaker_field(self):
        # 80 GB and "U2" both exist for the 5.5G, but the U2 edition only came with 30 GB.
        device = _with_sources(
            _offline_device(model_family="iPod", generation="5.5th Gen", capacity="80GB", color="U2"),
            model_family="sysinfo", generation="sysinfo", capacity="disk_size", color="sysinfo",
        )

        enrich(device)

        assert device.capacity == "80GB"
        assert device.color in ("White", "Black", "")
        assert device.color != "U2"


class TestUniqueColor:
    def test_single_color_generation_gets_its_color(self):
        by_generation: dict[tuple[str, str], set[str]] = {}
        for family, generation, _capacity, color in IPOD_MODELS.values():
            by_generation.setdefault((family, generation), set()).add(color)
        (family, generation), colors = next(
            (key, colors) for key, colors in sorted(by_generation.items()) if len(colors) == 1 and "" not in colors)
        device = _with_sources(_offline_device(model_family=family, generation=generation),
                               model_family="sysinfo", generation="sysinfo")

        enrich(device)

        assert device.color == colors.pop()
        assert device._field_sources["color"] == "model_table"


class TestLateFillIns:
    def _root(self, tmp_path: Path) -> Path:
        (tmp_path / "iPod_Control" / "Device").mkdir(parents=True)
        (tmp_path / "iPod_Control" / "Artwork").mkdir()
        (tmp_path / "iPod_Control" / "iTunes").mkdir()
        return tmp_path

    def test_hash_info_file_supplies_the_hash72_material(self, tmp_path: Path):
        root = self._root(tmp_path)
        rndpart, iv = bytes(range(1, 13)), bytes(range(100, 116))
        blob = b"HASHv0" + b"\x00" * 20 + rndpart + iv
        (root / "iPod_Control" / "Device" / "HashInfo").write_bytes(blob)
        device = _offline_device(path=str(root), model_number="MB147")

        enrich(device)

        assert (device.hash_info_rndpart, device.hash_info_iv) == (rndpart, iv)

    def test_artworkdb_lists_formats_when_the_catalog_cannot(self, tmp_path: Path):
        root = self._root(tmp_path)
        entries = b"".join(b"mhif" + struct.pack("<IIII", 124, 124, 0, fid) + b"\x00" * 104 for fid in (1028, 1029))
        listing = b"mhlf" + struct.pack("<II", 92, 2) + b"\x00" * 80 + entries
        dataset = b"mhsd" + struct.pack("<IIH", 96, 96 + len(listing), 3) + b"\x00" * 82 + listing
        head = b"mhfd" + struct.pack("<IIIII", 132, 132 + len(dataset), 0, 2, 1) + b"\x00" * 108
        (root / "iPod_Control" / "Artwork" / "ArtworkDB").write_bytes(head + dataset)
        device = _offline_device(path=str(root))  # family "iPod", generation unknown: catalog is ambiguous

        enrich(device)

        assert set(device.artwork_formats) == {1028, 1029}
        assert device.artwork_formats[1029] == (200, 200)
