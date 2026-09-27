"""Small identity helpers shared by the library layer."""

from __future__ import annotations

import math
import os
from pathlib import Path

__all__ = ["as_int", "path_key"]


def path_key(path: str | os.PathLike[str]) -> str:
    """A comparable identity for a host path.

    ``~`` is expanded and the path resolved (symlinks followed) when possible,
    falling back to a purely lexical absolute path; case and separators are
    normalized the way the host OS compares them.
    """
    expanded = Path(path).expanduser()
    try:
        absolute = str(expanded.resolve())
    except OSError:
        absolute = os.path.abspath(os.fspath(expanded))
    return os.path.normcase(absolute)


def as_int(value: object, default: int = 0) -> int:
    """Best-effort integer from cached or parsed data; *default* when it is not a number."""
    if isinstance(value, bool) or isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else default
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return int(value)
        except ValueError:
            return default
    return default
