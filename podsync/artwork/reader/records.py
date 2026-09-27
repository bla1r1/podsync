"""Parsers for every ArtworkDB chunk, dispatched by tag.

Each parser returns ``{"nextOffset": int, "result": ...}`` (datasets add
``"datasetType"``).  The result keys keep the ArtworkDB community names
(``img_id``, ``songId``, ``correlationID`` …) because downstream code and saved
dumps use them.
"""

from __future__ import annotations

import base64
import struct
from collections.abc import Callable
from typing import Any

from podsync.artwork.spec.format import CHUNK_TYPE_MAP
from podsync.artwork.spec.renditions import infer_image_format, read_mhni_fields
from podsync.artwork.spec.format import (
    decode_mhod_string_body,
    is_mhod_container,
    mhod_type_info,
    mhod_type_name,
)

__all__ = ["parse_chunk"]

Parser = Callable[[bytes, int, int, int], dict]


def _json_safe(value: Any) -> Any:
    """Raw bytes become base64 text so the parsed tree can be serialized."""
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _children(data: bytes, start: int, count: int):
    """Yield each child's parse result, advancing by its ``nextOffset``."""
    cursor = start
    for _ in range(count):
        child = parse_chunk(data, cursor)
        yield child
        cursor = child["nextOffset"]


_MHFD_WORDS = (
    ("<IIIII", 12, ("unk1", "unk2", "childCount", "unk3", "next_mhii_id")),
    ("<QQ", 32, ("unk4", "unk5")),
    ("<IIIII", 48, ("unk6", "unk7", "unk8", "unk9", "unk10")),
)


def _file_header(data: bytes, offset: int, header_length: int, _length: int) -> dict:
    """``mhfd``: the root; its datasets are stored under their kind names."""
    root: dict[str, Any] = {}
    for fmt, at, names in _MHFD_WORDS:
        root.update(zip(names, struct.unpack_from(fmt, data, offset + at)))
    cursor = offset + header_length
    for child in _children(data, cursor, root["childCount"]):
        root[CHUNK_TYPE_MAP[child["datasetType"]]] = child["result"]
        cursor = child["nextOffset"]
    return {"nextOffset": cursor, "result": _json_safe(root)}


def _dataset(data: bytes, offset: int, header_length: int, length: int) -> dict:
    """``mhsd``: one list, tagged with its dataset kind."""
    (kind,) = struct.unpack_from("<H", data, offset + 12)
    child = parse_chunk(data, offset + header_length)
    return {
        "datasetType": kind,
        "result": child.get("result", child) if isinstance(child, dict) else child,
        "nextOffset": offset + length,
    }


def _image_list(data: bytes, offset: int, header_length: int, image_count: int) -> dict:
    """``mhli``: the third header word is the image count, not a length."""
    images = []
    cursor = offset + header_length
    for child in _children(data, cursor, image_count):
        images.append(child["result"])
        cursor = child["nextOffset"]
    return {"nextOffset": cursor, "result": images}


def _image(data: bytes, offset: int, header_length: int, length: int) -> dict:
    """``mhii``: one artwork item and its per-format containers."""
    child_count, img_id, song_id = struct.unpack_from("<IIQ", data, offset + 12)
    unk1, rating, unk2, original_date, taken_date, source_size = struct.unpack_from("<6I", data, offset + 28)
    image: dict[str, Any] = {
        "img_id": img_id, "songId": song_id, "unk1": unk1, "rating": rating, "unk2": unk2,
        "originalDate": original_date, "exifTakenDate": taken_date, "srcImgSize": source_size,
    }
    for child in _children(data, offset + header_length, child_count):
        entry = child["result"]
        kind = entry.get("mhodType") if isinstance(entry, dict) else None
        name = mhod_type_name(kind) if isinstance(kind, int) else None
        if name is None:
            image.setdefault("_unknown_mhods", []).append(entry)
            continue
        image[name] = entry
        if is_mhod_container(kind):
            image.setdefault("_image_containers", []).append(entry)
    return {"nextOffset": offset + length, "result": image}


def _image_name(data: bytes, offset: int, header_length: int, length: int) -> dict:
    """``mhni``: where one rendition lives inside an ``.ithmb`` file and its geometry."""
    f = read_mhni_fields(data, offset)
    name: dict[str, Any] = {
        "correlationID": f.format_id,
        "ithmbOffset": f.ithmb_offset,
        "imgSize": f.image_size,
        "verticalPadding": f.vertical_padding,
        "horizontalPadding": f.horizontal_padding,
        "imageHeight": f.image_height,
        "imageWidth": f.image_width,
        "unk1": f.unk1,
        "imgSize2": f.image_size_2,
        "estimatedPixmapHeight": f.estimated_pixmap_height,
        "estimatedPixmapWidth": f.estimated_pixmap_width,
        "image_format": infer_image_format(f),
    }
    for child in _children(data, offset + header_length, f.child_count):
        entry = child.get("result", {})
        if isinstance(entry, dict) and "mhodType" in entry:
            name[entry["mhodType"]] = entry
    return {"nextOffset": offset + length, "result": name}


def _data_object(data: bytes, offset: int, header_length: int, length: int) -> dict:
    """``mhod``: a string (e.g. the ``.ithmb`` path) or a container of one ``mhni``."""
    (kind,) = struct.unpack_from("<H", data, offset + 12)
    info = mhod_type_info(kind)
    if info is None:
        entry: dict[str, Any] = {"mhodType": kind, "_unknown": True}
    elif info["type"] == "String":
        entry = {"mhodType": kind, info["name"]: decode_mhod_string_body(data, offset + header_length, offset + length)}
    elif info["type"] == "Container":
        entry = {"mhodType": kind, info["name"]: parse_chunk(data, offset + header_length)}
    else:
        entry = {"mhodType": "ERROR"}
    return {"nextOffset": offset + length, "result": entry}


def _skipped(*_args: Any) -> dict:
    # Album/file lists are not needed to resolve artwork; they are ignored.
    return {}


_PARSERS: dict[str, Parser] = {
    "mhfd": _file_header,
    "mhsd": _dataset,
    "mhli": _image_list,
    "mhii": _image,
    "mhni": _image_name,
    "mhod": _data_object,
    **{tag: _skipped for tag in ("mhla", "mhba", "mhia", "mhlf", "mhif", "mhaf")},
}


def parse_chunk(data: bytes, offset: int) -> dict:
    tag = bytes(data[offset:offset + 4]).decode("utf-8")
    header_length, third_word = struct.unpack_from("<II", data, offset + 4)
    parser = _PARSERS.get(tag)
    if parser is None:
        raise ValueError(f"Unknown chunk type: {tag}")
    return parser(data, offset, header_length, third_word)
