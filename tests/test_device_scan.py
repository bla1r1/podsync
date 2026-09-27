"""Mount discovery, alias collapsing, macOS ioreg pairing, model resolution."""

from podsync.hardware.discovery import scan
from podsync.hardware.current import IpodDevice
from podsync.hardware.discovery.macos import parse_ioreg_bsd_serials
from podsync.hardware.discovery.resolve import resolve_model

_SINGLE_IPOD_TREE = """\
 +-o iPod@01130000  <class IOUSBHostDevice, ...>
  |   "USB Serial Number" = "4D0C1E2F3A4B5C6D"
  |   "idProduct" = 4704
  +-o IOUSBMassStorageDriver  <class IOUSBMassStorageDriver, ...>
    +-o IOUSBMassStorageInterfaceNub  <class IOSCSIPeripheralDeviceType00, ...>
      +-o Apple iPod Media  <class IOMedia, ...>
      |   "BSD Name" = "disk7"
"""

_CROWDED_APPLE_BUS = """\
 +-o AppleUSBKeyboard@14100000  <class IOUSBHostDevice, ...>
  |   "USB Serial Number" = "KBD-778899"
  |   "idProduct" = 555
 +-o iPod@01130000  <class IOUSBHostDevice, ...>
  |   "USB Serial Number" = "4D0C1E2F3A4B5C6D"
  |   "idProduct" = 4704
  +-o IOUSBMassStorageDriver  <class IOUSBMassStorageDriver, ...>
    +-o IOUSBMassStorageInterfaceNub  <class IOSCSIPeripheralDeviceType00, ...>
      +-o Apple iPod Media  <class IOMedia, ...>
      |   "BSD Name" = "disk7"
 +-o AppleWirelessReceiver@14200000  <class IOUSBHostDevice, ...>
  |   "USB Serial Number" = "MICE-445566"
  |   "idProduct" = 999
"""

_TWO_IPODS_BEHIND_HUB = """\
 +-o AppleUSBHub@14000000  <class IOUSBHostDevice, ...>
  |   "USB Serial Number" = "HUB-002"
  |   "idProduct" = 100
  +-o iPod@14100000  <class IOUSBHostDevice, ...>
    |   "USB Serial Number" = "MMX111"
    |   "idProduct" = 4704
    +-o IOUSBMassStorageDriver  <class IOUSBMassStorageDriver, ...>
      +-o IOUSBMassStorageInterfaceNub  <class IOSCSIPeripheralDeviceType00, ...>
        +-o Apple iPod Media  <class IOMedia, ...>
        |   "BSD Name" = "disk7"
  +-o iPod@14200000  <class IOUSBHostDevice, ...>
    |   "USB Serial Number" = "NNY222"
    |   "idProduct" = 4704
    +-o IOUSBMassStorageDriver  <class IOUSBMassStorageDriver, ...>
      +-o IOUSBMassStorageInterfaceNub  <class IOSCSIPeripheralDeviceType00, ...>
        +-o Apple iPod Media  <class IOMedia, ...>
        |   "BSD Name" = "disk9"
"""

_PERIPHERAL_ONLY_BUS = """\
 +-o AppleUSBKeyboard@14100000  <class IOUSBHostDevice, ...>
  |   "USB Serial Number" = "KBD-778899"
  |   "idProduct" = 555
"""

_IPOD_WITH_SPACEY_SERIAL = """\
 +-o iPod@01130000  <class IOUSBHostDevice, ...>
  |   "USB Serial Number" = "x9 y8 z7 q6 w5"
  +-o IOUSBMassStorageDriver  <class IOUSBMassStorageDriver, ...>
    +-o Apple iPod Media  <class IOMedia, ...>
    |   "BSD Name" = "disk3"
"""


def _device(path: str, *, guid: str = "", serial: str = "") -> IpodDevice:
    return IpodDevice(
        path=path,
        mount_name=path.rsplit("/", 1)[-1],
        firewire_guid=guid,
        serial=serial,
    )


def test_scan_merges_alias_mounts_of_one_physical_device(monkeypatch) -> None:
    devices = {
        "/media/audrey/POD": _device(
            "/media/audrey/POD",
            guid="89ABCDEF01234567",
            serial="SERL90012",
        ),
        "/run/media/audrey/POD": _device(
            "/run/media/audrey/POD",
            guid="89abcdef01234567",
            serial="SERL90012",
        ),
    }

    monkeypatch.setattr(
        scan,
        "_find_ipod_volumes",
        lambda: [
            ("/media/audrey/POD", "POD"),
            ("/run/media/audrey/POD", "POD"),
        ],
    )
    monkeypatch.setattr(
        scan,
        "_identify_ipod_mount",
        lambda mount_path, _display_name: devices[mount_path],
    )
    monkeypatch.setattr(scan, "_clear_macos_usb_cache", lambda: None)

    assert scan.find_ipods() == [devices["/media/audrey/POD"]]


def test_scan_retains_separately_identified_devices(monkeypatch) -> None:
    first = _device("/media/audrey/POD_A", guid="FEEDFACE00112233")
    second = _device("/media/audrey/POD_B", guid="112233EEFF00AABB")

    monkeypatch.setattr(
        scan,
        "_find_ipod_volumes",
        lambda: [
            (first.path, "POD_A"),
            (second.path, "POD_B"),
        ],
    )
    monkeypatch.setattr(
        scan,
        "_identify_ipod_mount",
        lambda mount_path, _display_name: first if mount_path == first.path else second,
    )
    monkeypatch.setattr(scan, "_clear_macos_usb_cache", lambda: None)

    assert scan.find_ipods() == [first, second]


def test_linux_scan_under_sudo_looks_in_the_invoking_users_media_dir(monkeypatch) -> None:
    listings = {"/media/audrey": ["POD"]}

    def fake_listdir(path):
        if path not in listings:
            raise OSError(path)
        return listings[path]

    monkeypatch.setattr(scan.os, "listdir", fake_listdir)
    monkeypatch.setattr(scan, "_mounted_roots", lambda: [])
    monkeypatch.setenv("USER", "root")
    monkeypatch.setenv("SUDO_USER", "audrey")

    assert "/media/audrey/POD" in [path.replace("\\", "/") for path in scan._linux_candidate_roots()]


def test_linux_scan_finds_an_ipod_mounted_outside_the_usual_places(monkeypatch, tmp_path) -> None:
    pod = tmp_path / "srv" / "jukebox"
    (pod / "iPod_Control").mkdir(parents=True)
    monkeypatch.setattr(scan.sys, "platform", "linux")
    monkeypatch.setattr(scan.os, "listdir", lambda path: (_ for _ in ()).throw(OSError(path)))
    monkeypatch.setattr(scan, "_mounted_roots", lambda: [str(pod)])

    assert scan._find_ipod_volumes() == [(str(pod), "jukebox")]


def test_ioreg_text_pairs_disk_with_inline_ipod_serial() -> None:
    assert parse_ioreg_bsd_serials(_SINGLE_IPOD_TREE) == {
        "disk7": "4D0C1E2F3A4B5C6D",
    }


def test_ioreg_text_skips_unrelated_apple_devices() -> None:
    """Extra Apple peripherals on the bus never capture an iPod disk name.

    An earlier revision built parallel lists of Apple serials and iPod
    disks and zipped them strictly, so a keyboard or wireless receiver on
    the bus crashed identification.  Pairing the serial inline next to the
    media node avoids that mismatch entirely.
    """
    assert parse_ioreg_bsd_serials(_CROWDED_APPLE_BUS) == {
        "disk7": "4D0C1E2F3A4B5C6D",
    }


def test_ioreg_text_pairs_each_hub_attached_ipod_with_its_own_serial() -> None:
    """Every hub-attached iPod keeps its own serial, not the hub's."""
    assert parse_ioreg_bsd_serials(_TWO_IPODS_BEHIND_HUB) == {
        "disk7": "MMX111",
        "disk9": "NNY222",
    }


def test_ioreg_text_without_ipods_yields_empty_mapping() -> None:
    assert parse_ioreg_bsd_serials(_PERIPHERAL_ONLY_BUS) == {}


def test_ioreg_text_normalizes_serial_spacing_and_case() -> None:
    """Embedded spaces and lowercase letters collapse so both macOS maps
    can be cross-referenced against each other.
    """
    assert parse_ioreg_bsd_serials(_IPOD_WITH_SPACEY_SERIAL) == {
        "disk3": "X9Y8Z7Q6W5",
    }


def test_resolver_pins_nano_from_three_char_suffix() -> None:
    resolved = resolve_model(
        {"usb_pid": 0x1262, "model_family": "iPod Nano", "generation": "3rd Gen"},
        {"serial": "Q9X772613F"},
        disk_size_gb=7.4,
    )

    assert resolved["model_number"] == "MB453"
    assert resolved["model_family"] == "iPod Nano"
    assert resolved["generation"] == "3rd Gen"
    assert resolved["capacity"] == "8GB"
    assert resolved["color"] == "Pink"


def test_resolver_pins_nano_from_four_char_suffix() -> None:
    resolved = resolve_model(
        {"usb_pid": 0x1267, "model_family": "iPod Nano", "generation": "7th Gen"},
        {"serial": "C8P04100F0GD"},
        disk_size_gb=15.0,
    )

    assert resolved["model_number"] == "MD475"
    assert resolved["model_family"] == "iPod Nano"
    assert resolved["generation"] == "7th Gen"
    assert resolved["capacity"] == "16GB"
    assert resolved["color"] == "Pink"


def test_resolver_prefers_live_serial_with_empty_cache() -> None:
    resolved = resolve_model(
        {
            "serial": "ZZTOPPP2C7",
            "firewire_guid": "01A2B3C4D5E6F708",
            "usb_pid": 0x1261,
            "_sources": {
                "serial": "udev_scsi_id",
                "firewire_guid": "sysfs",
                "usb_pid": "sysfs",
            },
        },
        {},
        disk_size_gb=127.7,
    )

    assert resolved["serial"] == "ZZTOPPP2C7"
    assert resolved["model_number"] == "MB565"
    assert resolved["model_family"] == "iPod Classic"
    assert resolved["generation"] == "6.5th Gen"
    assert resolved["capacity"] == "120GB"
    assert resolved["color"] == "Black"


def test_resolver_rejects_stale_sysinfo_serial_in_conflict() -> None:
    resolved = resolve_model(
        {
            "serial": "ZZTOPPP2C7",
            "_sources": {"serial": "udev_scsi_id"},
        },
        {
            "serial": "C8P04100F0GD",
            "_sources": {"serial": "sysinfo"},
        },
        disk_size_gb=111.8,
    )

    assert resolved["serial"] == "ZZTOPPP2C7"
    assert resolved["model_number"] == "MB565"
    assert resolved["_sources"]["serial"] == "udev_scsi_id"
    assert any(
        conflict["field"] == "serial"
        and conflict["rejected_value"] == "C8P04100F0GD"
        for conflict in resolved["_conflicts"]
    )


def test_hardware_probe_carries_linux_evidence_sources(monkeypatch) -> None:
    monkeypatch.setattr(scan.sys, "platform", "linux")
    monkeypatch.setattr(
        scan,
        "linux_device_identity",
        lambda _mount: {
            "serial": "9Q551288Z3R",
            "firewire_guid": "01A2B3C4D5E6F708",
            "_sources": {
                "serial": "udev_scsi_id",
                "firewire_guid": "sysfs",
            },
        },
    )

    result = scan._probe_hardware("/media/wendy/POD", "POD")

    assert result["_sources"]["serial"] == "udev_scsi_id"
    assert result["_sources"]["firewire_guid"] == "sysfs"
    assert result["serial"] == "9Q551288Z3R"
    assert result["firewire_guid"] == "01A2B3C4D5E6F708"
