"""Walk the iTunesDB chunk tree.

Each chunk kind has a parser registered with :func:`handles`.  A parser gets
``(data, offset, header_length, third_word)`` — the third header word is the
chunk's total length for records and its child count for lists — and returns a
:data:`ParseResult`.  Unknown tags are kept as opaque header/body bytes and
summarised in one warning per parse.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from typing import Any

from podsync.itdb.reader.primitives import ParseResult, raw_capture_enabled, raw_span_info, read_chunk_header

__all__ = ["handles", "log_unknown_chunk_summary", "parse_children", "parse_chunk", "reset_unknown_chunk_summary"]

logger = logging.getLogger(__name__)

Parser = Callable[[bytes | bytearray, int, int, int], ParseResult]
_PARSERS: dict[str, Parser] = {}

_unknown_seen: Counter[tuple[str, int]] = Counter()
_SUMMARY_EXAMPLES = 5


def handles(*tags: str) -> Callable[[Parser], Parser]:
    """Register the decorated function as the parser for *tags*."""
    def register(parser: Parser) -> Parser:
        for tag in tags:
            _PARSERS[tag] = parser
        return parser
    return register


def reset_unknown_chunk_summary() -> None:
    _unknown_seen.clear()


def log_unknown_chunk_summary() -> None:
    if not _unknown_seen:
        return
    examples = ", ".join(
        f"{tag!r} at 0x{offset:X}" for (tag, offset), _n in _unknown_seen.most_common(_SUMMARY_EXAMPLES)
    )
    more = len(_unknown_seen) - _SUMMARY_EXAMPLES
    logger.warning(
        "iTunesDB contained %d unknown chunk(s); ignored while parsing. Examples: %s%s",
        sum(_unknown_seen.values()), examples, f"; +{more} more" if more > 0 else "",
    )


def parse_children(data: bytes | bytearray, offset: int, child_count: int) -> tuple[list[dict[str, Any]], int]:
    """Parse *child_count* consecutive chunks; returns wrappers and the offset after the last."""
    children: list[dict[str, Any]] = []
    for _ in range(child_count):
        result, tag = parse_chunk(data, offset)
        wrapper: dict[str, Any] = {"chunk_type": tag, "data": result["data"]}
        if "_raw_chunk" in result:
            wrapper["_raw_chunk"] = result["_raw_chunk"]
        children.append(wrapper)
        offset = result["next_offset"]
    return children, offset


@handles("mhlt", "mhla", "mhli", "mhlp")
def _parse_list(data: bytes | bytearray, offset: int, header_length: int, child_count: int) -> ParseResult:
    children, end = parse_children(data, offset + header_length, child_count)
    return {"next_offset": end, "data": children, "_body_end": end}


def _parse_opaque(data: bytes | bytearray, offset: int, tag: str, header_length: int, length: int) -> ParseResult:
    _unknown_seen[(tag, offset)] += 1
    return {
        "next_offset": offset + length,
        "data": {
            "chunk_type": tag,
            "header": bytes(data[offset:offset + header_length]),
            "body": bytes(data[offset + header_length:offset + length]),
        },
        "_body_end": offset + header_length,
    }


def parse_chunk(data: bytes | bytearray, offset: int) -> tuple[dict[str, Any], str]:
    """Parse the chunk at *offset*; returns ``(result, tag)``."""
    tag, header_length, third_word = read_chunk_header(data, offset)
    parser = _PARSERS.get(tag)
    if parser is None:
        result = _parse_opaque(data, offset, tag, header_length, third_word)
    else:
        result = parser(data, offset, header_length, third_word)
    body_end = result.pop("_body_end", result["next_offset"])
    if raw_capture_enabled():
        result["_raw_chunk"] = raw_span_info(
            data,
            offset=offset,
            header_length=header_length,
            declared_length_or_child_count=third_word,
            end_offset=result["next_offset"],
            parsed_body_end=body_end,
        )
    return result, tag


# The record parsers register themselves on import.
from podsync.itdb.reader.chunks import records as _records, strings as _strings  # noqa: E402,F401
