"""Writing the iTunesDB: records, chunk encoders, signatures and the device install.

``write_itdb`` is resolved through this package at call time by the library
layer, so it can be substituted here.
"""

from podsync.hardware import SignatureKind, detect_signature_kind, get_firewire_id
from podsync.itdb.spec.codes import (
    MEDIA_TYPE_AUDIO,
    MEDIA_TYPE_AUDIOBOOK,
    MEDIA_TYPE_MUSIC_VIDEO,
    MEDIA_TYPE_PODCAST,
    MEDIA_TYPE_RINGTONE,
    MEDIA_TYPE_TV_SHOW,
    MEDIA_TYPE_VIDEO,
    MEDIA_TYPE_VIDEO_PODCAST,
)
from podsync.itdb.writer.database import extract_db_info, write_itdb, write_mhbd
from podsync.itdb.writer.lists import write_mhii_artist, write_mhli, write_mhli_empty
from podsync.itdb.writer.playlist import PlaylistRecord, write_mhyp, write_playlist
from podsync.itdb.writer.signing.ab import compute_hashab, write_hashab
from podsync.itdb.writer.signing.aes72 import (
    compute_hash72,
    extract_hash_info,
    extract_hash_info_to_dict,
    read_hash_info,
    write_hash72,
)
from podsync.itdb.writer.signing.hmac58 import compute_hash58, write_hash58
from podsync.itdb.writer.smart_rules import NestedRules, SmartPrefs, SmartRule, SmartRuleSet, prefs_from_row, rules_from_row
from podsync.itdb.writer.track import TrackRecord, write_mhit

__all__ = [
    "MEDIA_TYPE_AUDIO", "MEDIA_TYPE_AUDIOBOOK", "MEDIA_TYPE_MUSIC_VIDEO", "MEDIA_TYPE_PODCAST",
    "MEDIA_TYPE_RINGTONE", "MEDIA_TYPE_TV_SHOW", "MEDIA_TYPE_VIDEO", "MEDIA_TYPE_VIDEO_PODCAST",
    "NestedRules", "PlaylistRecord", "SignatureKind", "SmartPrefs", "SmartRule", "SmartRuleSet", "TrackRecord",
    "compute_hash58", "compute_hash72", "compute_hashab", "detect_signature_kind", "extract_db_info",
    "extract_hash_info", "extract_hash_info_to_dict", "get_firewire_id", "prefs_from_row", "read_hash_info",
    "rules_from_row", "write_checksum", "write_hash58", "write_hash72", "write_hashab", "write_itdb",
    "write_mhbd", "write_mhii_artist", "write_mhit", "write_mhli", "write_mhli_empty", "write_mhyp",
    "write_playlist",
]


def write_checksum(itdb_data: bytearray, ipod_path: str) -> bool:
    """Sign an image in place with whatever scheme the device at *ipod_path* needs."""
    import podsync.hardware as hardware

    kind = hardware.detect_signature_kind(ipod_path)
    if kind == SignatureKind.NONE:
        return True
    signers = {
        SignatureKind.HASH58: lambda: write_hash58(itdb_data, hardware.get_firewire_id(ipod_path)),
        SignatureKind.HASH72: lambda: write_hash72(itdb_data, ipod_path),
        SignatureKind.HASHAB: lambda: write_hashab(itdb_data, b""),
    }
    if kind not in signers:
        raise ValueError(f"Unsupported checksum type: {kind}.")
    signers[kind]()
    return True
