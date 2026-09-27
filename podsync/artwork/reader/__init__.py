"""Reading the ArtworkDB (the index of cover-art renditions stored in ``.ithmb`` files)."""

from __future__ import annotations

from podsync.artwork.reader.records import parse_chunk

__all__ = ["parse_artworkdb", "parse_chunk"]


def parse_artworkdb(file) -> dict:
    """Parse an ArtworkDB path or open binary file into a nested dict."""
    if isinstance(file, str):
        with open(file, "rb") as handle:
            data = handle.read()
    elif hasattr(file, "read"):
        data = file.read()
    else:
        raise TypeError("file must be a path (str) or a file-like object")
    parsed = parse_chunk(data, 0)
    return parsed.get("result", parsed)
