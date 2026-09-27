"""The whole podsync package must import cleanly with only stdlib + pycryptodome."""

import importlib

PACKAGES = [
    "podsync",
    "podsync.itdb.reader",
    "podsync.itdb.spec",
    "podsync.itdb.writer",
    "podsync.artwork.reader",
    "podsync.artwork.spec",
    "podsync.artwork.writer",
    "podsync.hardware",
    "podsync.library",
    "podsync.library.database",
    "podsync.library.tracks",
    "podsync.library.playlists",
    "podsync.library.smart",
    "podsync.library.media_paths",
    "podsync.library.paths",
]


def test_every_package_imports():
    for name in PACKAGES:
        assert importlib.import_module(name) is not None, name
