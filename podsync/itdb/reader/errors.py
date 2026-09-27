"""Errors raised while reading an iTunesDB."""

from __future__ import annotations

__all__ = ["CorruptHeaderError", "ITunesDBParseError", "InsufficientDataError", "UnknownChunkTypeError"]


class ITunesDBParseError(Exception):
    """The database could not be parsed."""

    def __init__(self, offset: int, message: str) -> None:
        self.offset = offset
        super().__init__(message)


class CorruptHeaderError(ITunesDBParseError):
    def __init__(self, offset: int, detail: str) -> None:
        self.detail = detail
        super().__init__(offset, f"Corrupt header at offset 0x{offset:X}: {detail}")


class UnknownChunkTypeError(ITunesDBParseError):
    def __init__(self, offset: int, chunk_type: str) -> None:
        self.chunk_type = chunk_type
        super().__init__(offset, f"Unknown chunk type {chunk_type!r} at offset 0x{offset:X}")


class InsufficientDataError(ITunesDBParseError):
    def __init__(self, offset: int, needed: int, available: int) -> None:
        self.needed = needed
        self.available = available
        super().__init__(
            offset, f"Insufficient data at offset 0x{offset:X}: need {needed} bytes, only {available} available",
        )
