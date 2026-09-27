"""Reading the iTunesDB: the chunk tree, Play Counts, On-The-Go playlists and diagnostics.

``read_itdb`` is resolved through this package at call time by the library
layer, so it can be substituted here.
"""

from podsync.itdb.reader.entry import decompress_itunescdb, read_itdb
from podsync.itdb.reader.errors import (
    CorruptHeaderError,
    InsufficientDataError,
    ITunesDBParseError,
    UnknownChunkTypeError,
)
from podsync.itdb.reader.play_stats import PlayStatsEntry, apply_play_stats, read_play_stats

__all__ = [
    "CorruptHeaderError",
    "ITunesDBParseError",
    "InsufficientDataError",
    "PlayStatsEntry",
    "UnknownChunkTypeError",
    "apply_play_stats",
    "decompress_itunescdb",
    "read_itdb",
    "read_play_stats",
]
