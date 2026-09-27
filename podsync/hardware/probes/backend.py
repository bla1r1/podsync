"""Finding a libusb-1.0 library for PyUSB.

PyUSB's own lookup covers system installs.  When it fails, the candidates are,
in order: the ``PODSYNC_LIBUSB_DLL`` / ``PYUSB_LIBUSB_DLL`` environment
variables, the ``libusb_package`` wheel, ``ctypes.util.find_library``, a copy
vendored under ``podsync/vendor/libusb/<os>`` and one next to the Python
executable.  PyUSB is optional: without it everything here returns ``None``.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import sys
from pathlib import Path

__all__ = ["backend_diagnostic", "get_libusb_backend"]

logger = logging.getLogger(__name__)

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_ENV_OVERRIDES = ("PODSYNC_LIBUSB_DLL", "PYUSB_LIBUSB_DLL")


def _libusb1_module():
    try:
        import usb.backend.libusb1 as libusb1  # type: ignore[import-not-found]
    except ImportError:
        return None
    return libusb1


def _packaged_library() -> str:
    try:
        import libusb_package  # type: ignore[import-not-found]

        return str(libusb_package.find_library() or "")
    except Exception:
        return ""


def _bundled_paths() -> list[Path]:
    exe_dir = Path(sys.executable).resolve().parent
    vendor = _PACKAGE_ROOT / "vendor" / "libusb"
    if sys.platform == "win32":
        arch = "x64" if sys.maxsize > 2**32 else "x86"
        return [vendor / "windows" / arch / "libusb-1.0.dll", exe_dir / "libusb-1.0.dll",
                exe_dir / "vendor" / "libusb" / "windows" / arch / "libusb-1.0.dll"]
    if sys.platform == "darwin":
        return [vendor / "macos" / "libusb-1.0.dylib", exe_dir / "libusb-1.0.dylib"]
    return [vendor / "linux" / "libusb-1.0.so", exe_dir / "libusb-1.0.so"]


def _candidate_paths() -> list[str]:
    """Library paths to try, without case-insensitive duplicates, first spelling kept."""
    candidates = [os.environ.get(name, "").strip() for name in _ENV_OVERRIDES]
    candidates += [_packaged_library(), ctypes.util.find_library("usb-1.0") or ""]
    candidates += [str(path) for path in _bundled_paths()]
    unique: dict[str, str] = {}
    for candidate in candidates:
        if candidate:
            unique.setdefault(candidate.casefold(), candidate)
    return list(unique.values())


def _load(libusb1, path: str | None):
    try:
        if path is None:
            return libusb1.get_backend()
        return libusb1.get_backend(find_library=lambda _name, path=path: path)
    except Exception:
        return None


def get_libusb_backend():
    """A PyUSB libusb1 backend, or ``None`` when PyUSB or the library is missing."""
    libusb1 = _libusb1_module()
    if libusb1 is None:
        return None
    backend = _load(libusb1, None)
    if backend is not None:
        return backend
    for candidate in _candidate_paths():
        if os.path.exists(candidate) and (backend := _load(libusb1, candidate)) is not None:
            logger.debug("Loaded libusb backend from %s", candidate)
            return backend
    return None


def backend_diagnostic() -> str:
    """One line explaining why a backend is or is not available (for diagnostic dumps)."""
    libusb1 = _libusb1_module()
    if libusb1 is None:
        return "pyusb is not installed"
    if _load(libusb1, None) is not None:
        return "system libusb backend available"
    candidates = _candidate_paths()
    if not candidates:
        return "no libusb-1.0 library candidates found"
    existing = [path for path in candidates if os.path.exists(path)]
    if existing:
        return "libusb candidates exist but failed to load: " + ", ".join(existing)
    return "libusb candidates missing: " + ", ".join(candidates)
