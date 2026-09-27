"""Keep untrusted paths inside the directory they are meant for.

Paths coming from a device database or from saved settings are attacker- or
corruption-controlled.  They are accepted only when they are relative (device)
or absolute below a root (host), contain no ``..``/drive/stream tricks, pass
through no symbolic link or reparse point, and still land inside the allowed
directory after resolution.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

__all__ = ["PathEscapeError", "UnsafeHostPathError", "resolve_host_path", "safe_device_path"]

_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


class PathEscapeError(ValueError):
    """A device-relative path would escape its allowed iPod subtree."""


class UnsafeHostPathError(ValueError):
    """A persisted host path would escape its allowed root."""


def _capitalized(label: str) -> str:
    return label[:1].upper() + label[1:]


def _relative_parts(value, label: str, error: type[ValueError] = PathEscapeError) -> tuple[str, ...]:
    try:
        raw = os.fspath(value)
    except TypeError:
        raise error(f"Invalid {label}") from None
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise error(f"Invalid {label}")
    text = raw.replace("\\", "/")
    if text.startswith("/") or _DRIVE_PREFIX.match(text):
        raise error(f"{_capitalized(label)} must be relative")
    parts = tuple(text.split("/"))
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        raise error(f"Invalid component in {label}: {raw}")
    return parts


def _refuse_links(root: Path, parts: tuple[str, ...], label: str, error: type[ValueError]) -> None:
    """Walk the components that exist and refuse any symlink or reparse point."""
    current = root
    for part in parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise error(f"Could not safely inspect {label} component {current}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or (getattr(info, "st_file_attributes", 0) or 0) & _REPARSE_POINT:
            raise error(f"{_capitalized(label)} contains a symbolic link or reparse point: {current}")


def safe_device_path(ipod_root: str | Path, device_relative_path: str | Path, *, allowed_subtree: str | Path) -> Path:
    """Resolve ``root/device_relative_path``, refusing anything outside ``root/allowed_subtree``."""
    parts = _relative_parts(device_relative_path, "device path")
    allowed_parts = _relative_parts(allowed_subtree, "allowed subtree")
    root = Path(ipod_root).resolve(strict=False)
    subtree, target = root.joinpath(*allowed_parts), root.joinpath(*parts)

    def check(allowed: Path, candidate: Path) -> None:
        if not allowed.is_relative_to(root):
            raise PathEscapeError("Allowed iPod subtree resolves outside the device root")
        if not candidate.is_relative_to(allowed):
            raise PathEscapeError(f"Device path is outside the allowed iPod subtree: {device_relative_path}")

    check(subtree, target)  # lexically
    _refuse_links(root, parts, "device path", PathEscapeError)
    resolved = target.resolve(strict=False)
    check(subtree.resolve(strict=False), resolved)  # and after resolution
    return resolved


def resolve_host_path(allowed_root: str | Path, persisted_path: str | Path) -> Path:
    """Resolve a saved absolute host path, refusing anything that is not strictly below *allowed_root*."""
    try:
        raw = os.fspath(persisted_path)
    except TypeError:
        raise UnsafeHostPathError("Invalid persisted host path") from None
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise UnsafeHostPathError("Invalid persisted host path")
    if not os.path.isabs(raw):
        raise UnsafeHostPathError("Persisted host path must be absolute")
    root = Path(os.path.abspath(allowed_root))
    target = Path(os.path.abspath(raw))
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise UnsafeHostPathError(f"Persisted host path is outside the allowed root: {raw}") from None
    if not relative.parts:
        raise UnsafeHostPathError("Persisted host path must name a file below the root")
    _refuse_links(root, relative.parts, "host path", UnsafeHostPathError)
    resolved = target.resolve(strict=False)
    if not resolved.is_relative_to(root.resolve(strict=False)):
        raise UnsafeHostPathError(f"Persisted host path escapes the allowed root: {raw}")
    return resolved
