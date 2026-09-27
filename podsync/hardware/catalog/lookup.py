"""Answering "which iPod is this?" from a model number or a serial number."""

from __future__ import annotations

import re

from podsync.hardware.catalog.models import IPOD_MODELS, SERIAL_SUFFIX_TO_MODEL, canonicalize_model_identity

__all__ = [
    "extract_model_number", "get_friendly_model_name", "get_model_info", "infer_generation", "lookup_by_serial",
    "match_serial_suffix", "usb_pid_identity_conflicts",
]

_MODEL_NUMBER = re.compile(r"M[A-Z]?\d{3,4}")
ModelRow = tuple[str, str, str, str]  # family, generation, capacity, color


def extract_model_number(model_str: str | None) -> str | None:
    """Normalize an order number (``"MA446LL/A"``, ``"xA446"``) to its model part (``"MA446"``)."""
    text = str(model_str or "").strip()
    if not text:
        return None
    if text.startswith("x"):  # SysInfo writes the leading "M" as "x"
        text = "M" + text[1:]
    upper = text.upper()
    for candidate in (upper, "M" + upper[1:]):
        match = _MODEL_NUMBER.match(candidate)
        if match:
            return match.group(0)
    return upper[:5]


def get_model_info(model_number: str | None) -> ModelRow | None:
    """Catalog row for a model number: exact, then with a forced "M", then by 4-character prefix."""
    number = str(model_number or "").strip().upper()
    if not number:
        return None
    row = IPOD_MODELS.get(number) or (None if number.startswith("M") else IPOD_MODELS.get("M" + number[1:]))
    if row is not None:
        return row
    return next((value for key, value in IPOD_MODELS.items() if number.startswith(key[:4])), None)


def get_friendly_model_name(model_number: str | None) -> str:
    """Readable model name such as ``iPod 5.5th Gen 30GB Black``."""
    row = get_model_info(model_number)
    if row is None:
        return f"Unknown iPod ({model_number})" if model_number else "Unknown iPod"
    return " ".join(part for part in row if part)


def match_serial_suffix(serial: str | None) -> str | None:
    """The longest known model suffix the serial number ends with."""
    text = str(serial or "").strip().upper()
    if not text:
        return None
    for length in sorted({len(key) for key in SERIAL_SUFFIX_TO_MODEL}, reverse=True):
        if len(text) >= length and text[-length:] in SERIAL_SUFFIX_TO_MODEL:
            return text[-length:]
    return None


def lookup_by_serial(serial: str | None) -> tuple[str, ModelRow] | None:
    """``(model number, catalog row)`` for a serial's suffix, or ``None``."""
    suffix = match_serial_suffix(serial)
    model = SERIAL_SUFFIX_TO_MODEL[suffix] if suffix else None
    row = IPOD_MODELS.get(model) if model else None
    return (model, row) if row is not None else None


def infer_generation(family: str, capacity: str = "") -> str | None:
    """A generation that is unambiguous for *family* (optionally narrowed by capacity)."""
    from podsync.hardware.catalog.capabilities import _FAMILY_GEN_CAPABILITIES

    family = canonicalize_model_identity(family, "")[0]
    generations = [gen for fam, gen in _FAMILY_GEN_CAPABILITIES if fam == family]
    if len(generations) == 1:
        return generations[0]
    if capacity:
        wanted = str(capacity).strip().upper()
        matches = {row[1] for row in IPOD_MODELS.values() if row[0] == family and row[2].upper() == wanted}
        if len(matches) == 1:
            return matches.pop()
    return None


# Pairs a USB product id legitimately reports for a different (but compatible) generation.
_COMPATIBLE_PID_GENERATIONS = {
    ("5th gen", "5.5th gen"),
    ("4th gen (photo)", "4th gen (photo)"),
    ("4th gen (photo)", "4th gen (color)"),
}


def usb_pid_identity_conflicts(model_family: str, generation: str, pid_family: str, pid_generation: str) -> bool:
    """True when the USB product id clearly describes a different model than the one identified."""
    family, gen, _ = (s.casefold() if isinstance(s, str) else s for s in canonicalize_model_identity(model_family, generation))
    pid_fam, pid_gen, _ = (s.casefold() if isinstance(s, str) else s for s in canonicalize_model_identity(pid_family, pid_generation))
    if not family or not pid_fam or (pid_fam == "ipod" and not pid_gen):
        return False
    if family != pid_fam:
        return True
    if not gen or not pid_gen or gen == pid_gen:
        return False
    return (pid_gen, gen) not in _COMPATIBLE_PID_GENERATIONS
