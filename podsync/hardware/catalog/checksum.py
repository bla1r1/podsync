"""Which database signature a model requires, and how the MHBD header numbers it."""

from __future__ import annotations

from enum import IntEnum

__all__ = ["CHECKSUM_MHBD_SCHEME", "MHBD_SCHEME_TO_CHECKSUM", "SignatureKind"]


class SignatureKind(IntEnum):
    """Database signature schemes, as stored in ``checksum_type``."""

    NONE = 0  # pre-2007 models: no signature
    HASH58 = 1  # Classic, nano 3G/4G
    HASH72 = 2  # nano 5G
    HASHAB = 3  # nano 6G/7G (not supported here)
    UNSUPPORTED = 98
    UNKNOWN = 99  # the model could not be identified


# The header's hashing_scheme word does not reuse the enum values for HASHAB.
CHECKSUM_MHBD_SCHEME: dict[SignatureKind, int] = {
    SignatureKind.NONE: 0,
    SignatureKind.HASH58: 1,
    SignatureKind.HASH72: 2,
    SignatureKind.HASHAB: 4,
}
MHBD_SCHEME_TO_CHECKSUM: dict[int, SignatureKind] = {word: kind for kind, word in CHECKSUM_MHBD_SCHEME.items()}
