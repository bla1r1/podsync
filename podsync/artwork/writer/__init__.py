"""Writing cover art: the ArtworkDB index and the ``.ithmb`` pixel files it points into."""

from podsync.artwork.writer.covers import art_hash, extract_art
from podsync.artwork.writer.pipeline import write_artworkdb
from podsync.artwork.writer.pixels import (
    ALL_KNOWN_FORMATS,
    IPOD_4G_PHOTO_FORMATS,
    IPOD_5G_FORMATS,
    IPOD_CLASSIC_FORMATS,
    IPOD_NANO_1G2G_FORMATS,
    IPOD_NANO_4G_FORMATS,
    IPOD_NANO_5G_FORMATS,
    convert_art_for_ipod,
    get_artwork_formats,
    image_from_bytes,
    rgb888_to_rgb565,
)
from podsync.artwork.writer.records import ArtworkEntry
from podsync.hardware import ITHMB_FORMAT_MAP, ITHMB_SIZE_MAP, ithmb_formats_for_device

__all__ = [
    "ALL_KNOWN_FORMATS", "IPOD_4G_PHOTO_FORMATS", "IPOD_5G_FORMATS", "IPOD_CLASSIC_FORMATS",
    "IPOD_NANO_1G2G_FORMATS", "IPOD_NANO_4G_FORMATS", "IPOD_NANO_5G_FORMATS", "ITHMB_FORMAT_MAP",
    "ITHMB_SIZE_MAP", "ArtworkEntry", "art_hash", "convert_art_for_ipod", "extract_art", "get_artwork_formats",
    "image_from_bytes", "ithmb_formats_for_device", "rgb888_to_rgb565", "write_artworkdb",
]
