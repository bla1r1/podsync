"""Parsers for the fixed-header records: database, dataset, track, playlist, entry, album, artist."""

from __future__ import annotations

from podsync.itdb.reader.primitives import ParseResult
from podsync.itdb.reader.walker import handles, parse_children
from podsync.itdb.spec.fields import decode_fields
from podsync.itdb.spec.playlists.kinds import PLAYLIST_KIND_FOLDER, PLAYLIST_KIND_PODCAST

__all__ = ["GENIUS_DATASET_TYPE", "parse_dataset", "parse_playlist", "parse_record"]

GENIUS_DATASET_TYPE = 9


def parse_record(data: bytes | bytearray, offset: int, header_length: int, chunk_length: int, *, tag: str) -> ParseResult:
    """Decode the header of *tag* and parse its ``child_count`` children."""
    fields = decode_fields(data, offset, tag, header_length)
    fields["children"], body_end = parse_children(data, offset + header_length, fields["child_count"])
    return {"next_offset": offset + chunk_length, "data": fields, "_body_end": body_end}


def _register_plain(tag: str) -> None:
    def parser(data, offset, header_length, chunk_length):
        return parse_record(data, offset, header_length, chunk_length, tag=tag)
    parser.__name__ = f"parse_{tag}"
    handles(tag)(parser)


for _tag in ("mhbd", "mhit", "mhip", "mhia", "mhii"):
    _register_plain(_tag)
del _tag


@handles("mhsd")
def parse_dataset(data: bytes | bytearray, offset: int, header_length: int, chunk_length: int) -> ParseResult:
    """A dataset holds exactly one list chunk — except Genius (type 9), kept as opaque bytes."""
    fields = decode_fields(data, offset, "mhsd", header_length)
    body_start, end = offset + header_length, offset + chunk_length
    if fields["dataset_type"] == GENIUS_DATASET_TYPE:
        payload = bytes(data[body_start:end])
        try:
            cuid = payload.decode("ascii")
        except UnicodeDecodeError:
            cuid = payload.hex()
        fields.update(raw_payload=payload, genius_cuid=cuid, children=[])
        return {"next_offset": end, "data": fields, "_body_end": body_start}
    fields["children"], body_end = parse_children(data, body_start, 1)
    return {"next_offset": end, "data": fields, "_body_end": body_end}


@handles("mhyp")
def parse_playlist(data: bytes | bytearray, offset: int, header_length: int, chunk_length: int) -> ParseResult:
    """Playlist header, then its data objects, then its entries (two separate child runs)."""
    fields = decode_fields(data, offset, "mhyp", header_length)
    kind = fields["playlist_kind_flags"]
    fields.update(
        podcast_flag=kind,
        unk0x30_playlist_ref=fields["parent_folder_playlist_id"],
        is_podcast=bool(kind & PLAYLIST_KIND_PODCAST),
        is_folder=bool(kind & PLAYLIST_KIND_FOLDER),
    )
    fields["mhod_children"], cursor = parse_children(data, offset + header_length, fields["mhod_child_count"])
    fields["mhip_children"], cursor = parse_children(data, cursor, fields["mhip_child_count"])
    return {"next_offset": offset + chunk_length, "data": fields, "_body_end": cursor}
