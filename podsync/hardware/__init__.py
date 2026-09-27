"""The iPod as an object: identity, capabilities, discovery and write safety.

Other packages look these names up through ``podsync.hardware`` at call time,
so tests can patch them at package level.
"""

from podsync.hardware.catalog.artwork import (
    ARTWORK_FORMATS_BY_ID,
    ITHMB_FORMAT_MAP,
    ITHMB_SIZE_MAP,
    cover_art_format_definitions_for_device,
    ithmb_formats_for_device,
    photo_formats_for_device,
    resolve_cover_art_format_definitions,
    resolve_cover_art_format_definitions_for_device,
)
from podsync.hardware.bootstrap import ensure_device_itunes_database
from podsync.hardware.catalog.capabilities import (
    ArtworkFormat,
    ModelTraits,
    traits_for_model,
    checksum_type_for_family_gen,
    cover_art_formats_for_family_gen,
)
from podsync.hardware.catalog.checksum import CHECKSUM_MHBD_SCHEME, MHBD_SCHEME_TO_CHECKSUM, SignatureKind
from podsync.hardware.current import (
    IpodDevice,
    UnidentifiedDeviceError,
    clear_current_device,
    detect_signature_kind,
    selected_device,
    selected_device_at,
    get_firewire_id,
    has_exact_model_number,
    database_filename_for_write,
    read_sysinfo,
    require_exact_model_number,
    locate_database,
    select_device,
)
from podsync.hardware.enrich import enrich
from podsync.hardware.catalog.lookup import (
    extract_model_number,
    get_friendly_model_name,
    get_model_info,
    infer_generation,
    lookup_by_serial,
    match_serial_suffix,
)
from podsync.hardware.catalog.models import (
    IPOD_MODELS,
    IPOD_RECOVERY_USB_PIDS,
    IPOD_USB_PIDS,
    SERIAL_LAST3_TO_MODEL,
    SERIAL_SUFFIX_TO_MODEL,
    USB_PID_TO_MODEL,
    canonicalize_model_identity,
)
from podsync.hardware.discovery.scan import identify_mounted_ipod, find_ipods
from podsync.hardware.catalog.sysinfo import (
    DeviceEvidence,
    EvidenceValue,
    ParsedSysInfoExtended,
    identity_from_sysinfo,
    identity_from_sysinfo_extended,
    parse_sysinfo_extended,
    parse_sysinfo_text,
)
from podsync.hardware.virtual.device import (
    VIRTUAL_IPOD_INFO_FILENAME,
    available_virtual_ipod_models,
    make_virtual_ipod,
    ensure_virtual_itunes_database,
    has_virtual_ipod_info,
    load_virtual_ipod_info,
    virtual_ipod_info_path,
)
from podsync.hardware.probes.libusb import identify_by_vpd
from podsync.hardware.probes.libusb import query_all_ipods as usb_query_all_ipods
from podsync.hardware.probes.libusb import query_ipod_vpd as usb_query_ipod_vpd
from podsync.hardware.probes.libusb import write_sysinfo as usb_write_sysinfo
from podsync.hardware.probes.usb_control import (
    query_all_ipod_usb_sysinfo_extended,
    query_ipod_usb_sysinfo_extended,
)

try:
    from podsync.hardware.probes.linux import query_ipod_vpd_for_path as linux_query_ipod_vpd_for_path
except ImportError:
    pass

try:
    from podsync.hardware.probes.windows import query_ipod_vpd_for_path as windows_query_ipod_vpd_for_path
except ImportError:
    pass
